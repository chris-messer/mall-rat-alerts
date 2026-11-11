"""Alert composition shared by the private monitor and the public composer.

The model is asked for JSON {"notify", "what_changed", "note"} (the public prompt
asks for "signoff" instead of "note"; an optional "signoff" is parsed and capped). Code (not the prompt) enforces
the final shape:

    <what_changed>
    <blank line>
    <note>

Each field is flattened to one line, sanitized for SMS, and fitted to the template's length
budget (the note is trimmed before the description). Invalid output moves on to the next model,
and the caller falls back to a deterministic template when every model fails.

This file is kept identical in fob-site-monitor/lambda/ and mall-rat-alerts/lambda/common/.
"""

import json
import logging
import re
import time

try:
    from common import sms_text
except ImportError:
    import sms_text

log = logging.getLogger()

SEP = "\n\n"
MIN_NOTE = 12
SIGNOFF_MAX = 40  # units; the optional model "signoff" is capped to this
# Messages never talk about how they were written.
_SELF_REFERENCE = re.compile(
    r"\b(AI|A\.I\.|artificial intelligence|language model|LLM|chat ?bot|GPT|Claude|Bedrock|"
    r"auto-?generated|written by (a )?(bot|model|machine))\b",
    re.I,
)
_TRUE = (True, "true", "yes", 1)
_FALSE = (False, "false", "no", 0)


class InvalidOutput(ValueError):
    pass


def one_line(value):
    return re.sub(r"\s+", " ", value or "").strip()


def measure_for(mode):
    return sms_text.units if mode == "public" else len


def parse_model_json(text, mode):
    """Validate the model's JSON. Returns {"notify", "what_changed", "note"} (sanitized) or raises."""
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        raise InvalidOutput("no JSON object")
    raw = m.group(0)
    try:
        obj = json.loads(raw)
    except ValueError:
        # Models sometimes emit \' or other escapes JSON doesn't allow.
        try:
            obj = json.loads(re.sub(r'\\(?!["\\/bfnrtu])', "", raw))
        except ValueError as e:
            raise InvalidOutput(f"bad JSON: {e}") from None
    if not isinstance(obj, dict):
        raise InvalidOutput("not an object")
    what, note, notify = obj.get("what_changed"), obj.get("note", ""), obj.get("notify", True)
    if not isinstance(what, str) or not isinstance(note, str):
        raise InvalidOutput("what_changed and note must be strings")
    if isinstance(notify, str):
        notify = notify.strip().lower()
    if notify in _TRUE:
        notify = True
    elif notify in _FALSE:
        notify = False
    else:
        raise InvalidOutput("notify must be a boolean")
    what = sms_text.sanitize(one_line(what), mode)
    note = sms_text.sanitize(one_line(note), mode)
    if not what:
        raise InvalidOutput("empty what_changed")
    if _SELF_REFERENCE.search(what) or _SELF_REFERENCE.search(note):
        raise InvalidOutput("mentions how the message was written")
    return {"notify": notify, "what_changed": what, "note": note, "signoff": clean_signoff(obj.get("signoff"), mode)}


def clean_signoff(value, mode):
    """Optional model "signoff": sanitized, one line, capped at SIGNOFF_MAX. "" when absent or unusable."""
    if not isinstance(value, str):
        return ""
    out = sms_text.sanitize(one_line(value), mode)
    if not out or _SELF_REFERENCE.search(out):
        return ""
    return trim_to(out, SIGNOFF_MAX, measure_for(mode))


def trim_to(text, limit, measure):
    """Cut at a word boundary so measure(text) <= limit, adding "..." when cut."""
    if measure(text) <= limit:
        return text
    if limit <= 3:
        return ""
    words = text.split(" ")
    while words and measure(" ".join(words) + "...") > limit:
        words.pop()
    if not words:
        out = text
        while out and measure(out + "...") > limit:
            out = out[:-1]
        return (out.rstrip() + "...") if out else ""
    return " ".join(words).rstrip(" ,;:-") + "..."


def fit(what, note, max_units, target_units, measure):
    """Fit description + note into the budget. Prefer target_units (one SMS segment for public
    messages) by trimming only the note; otherwise use max_units, trimming the note first and the
    description last."""
    sep = measure(SEP)

    def total(w, n):
        return measure(w) + (sep + measure(n) if n else 0)

    target_units = min(target_units or max_units, max_units)
    if total(what, note) <= target_units:
        return what, note
    room = target_units - measure(what) - sep
    if note and room >= MIN_NOTE:
        return what, trim_to(note, room, measure)
    if total(what, note) <= max_units:
        return what, note
    room = max_units - measure(what) - sep
    if note and room >= MIN_NOTE:
        return what, trim_to(note, room, measure)
    note = trim_to(note, min(measure(note), 40), measure) if note else ""
    room = max_units - (sep + measure(note) if note else 0)
    return trim_to(what, room, measure), note


def assemble(what, note, mode, max_chars, target_chars=None):
    """Final SMS body and its stats. Always "what\\n\\nnote" (or just what if there is no note)."""
    what = sms_text.sanitize(one_line(what), mode)
    note = sms_text.sanitize(one_line(note), mode)
    what, note = fit(what, note, int(max_chars), int(target_chars or max_chars), measure_for(mode))
    body = what + SEP + note if note else what
    return body, sms_text.stats(body)


def _status_of(err):
    return ((getattr(err, "response", None) or {}).get("ResponseMetadata") or {}).get("HTTPStatusCode")


def compose(bedrock, trace, template, user_text, model_ids, mode, extra_props=None):
    """Try each model in order. Every attempt is logged to the trace as its own generation.

    Returns {"model", "notify", "what_changed", "note", "signoff", "attempts"} or None if every model failed.
    """
    system = template["system_prompt"]
    temperature = float(template.get("temperature", 0.2))
    max_tokens = int(template.get("max_tokens", 600))
    extra_props = extra_props or {}
    for attempt, model in enumerate(model_ids, 1):
        t0 = time.time()
        common = dict(temperature=temperature, max_tokens=max_tokens, attempt=attempt, **extra_props)
        try:
            r = bedrock.converse(
                modelId=model,
                system=[{"text": system}],
                messages=[{"role": "user", "content": [{"text": user_text}]}],
                inferenceConfig={"maxTokens": max_tokens, "temperature": temperature},
            )
        except Exception as e:
            trace.generation(model, system, user_text, latency=time.time() - t0,
                             http_status=_status_of(e) or 0, error=f"{type(e).__name__}: {e}", **common)
            log.warning("bedrock %s failed: %s", model, e)
            continue
        latency = time.time() - t0
        usage = r.get("usage") or {}
        text = "".join(c.get("text", "") for c in r["output"]["message"].get("content", []))
        gen = dict(output_text=text, input_tokens=usage.get("inputTokens"), output_tokens=usage.get("outputTokens"),
                   latency=latency, http_status=_status_of(r) or 200, stop_reason=r.get("stopReason"), **common)
        try:
            parts = parse_model_json(text, mode)
        except InvalidOutput as e:
            trace.generation(model, system, user_text, error=f"invalid output: {e}", **gen)
            log.warning("bedrock %s returned invalid output: %s", model, e)
            continue
        trace.generation(model, system, user_text, **gen)
        return {"model": model, "attempts": attempt, **parts}
    return None
