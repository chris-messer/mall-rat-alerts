"""PostHog LLM analytics ($ai_generation / $ai_trace) over stdlib urllib.

One Trace per alert. Every Bedrock attempt (fallbacks included) is recorded as its own
$ai_generation under the same $ai_trace_id, then everything goes out in one /batch/ request
with a short timeout. Failures are logged and swallowed: tracing must never break a send.

Never put phone numbers, Twilio credentials, or secrets in here. Text fields are scrubbed of
anything that looks like a phone number before sending.

This file is kept identical in fob-site-monitor/lambda/ and mall-rat-alerts/lambda/common/.
"""

import json
import logging
import re
import time
import urllib.request
import uuid
from datetime import datetime, timezone

log = logging.getLogger()

_PHONE = re.compile(r"(?<![\w/])\+?1?[\s.(-]{0,2}\d{3}[\s.)-]{0,2}\d{3}[\s.-]?\d{4}(?!\w)")
MAX_TEXT = 20000


def scrub(value):
    if isinstance(value, str):
        return _PHONE.sub("[phone]", value)[:MAX_TEXT]
    if isinstance(value, list):
        return [scrub(v) for v in value]
    if isinstance(value, dict):
        return {k: scrub(v) for k, v in value.items()}
    return value


def _iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z")


def _post(url, body, timeout):
    data = json.dumps(body, default=lambda o: float(o) if hasattr(o, "as_integer_ratio") else str(o)).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status


class Trace:
    def __init__(self, key, host, app, distinct_id, span_name, properties=None, post=None, timeout=3):
        self.key = key or ""
        self.host = (host or "https://us.i.posthog.com").rstrip("/")
        self.app = app
        self.distinct_id = distinct_id
        self.span_name = span_name
        self.properties = {k: v for k, v in (properties or {}).items() if v is not None}
        self.trace_id = str(uuid.uuid4())
        self.started = time.time()
        self.events = []
        self._post = post
        self._timeout = timeout

    def _base(self):
        return {
            "distinct_id": self.distinct_id,
            "$process_person_profile": False,
            "$ai_trace_id": self.trace_id,
            "$ai_session_id": None,
            "app": self.app,
            **self.properties,
        }

    def generation(self, model, system, user_text, output_text=None, input_tokens=None, output_tokens=None,
                   latency=None, http_status=None, error=None, temperature=None, max_tokens=None,
                   stop_reason=None, attempt=None, **extra):
        props = {
            **self._base(),
            "$ai_span_id": str(uuid.uuid4()),
            "$ai_parent_id": self.trace_id,
            "$ai_span_name": f"{self.span_name}:{model}",
            "$ai_model": model,
            "$ai_provider": "bedrock",
            "$ai_input": scrub([
                {"role": "system", "content": system},
                {"role": "user", "content": user_text},
            ]),
            "$ai_output_choices": scrub([{"role": "assistant", "content": output_text}]) if output_text else [],
            "$ai_input_tokens": input_tokens,
            "$ai_output_tokens": output_tokens,
            "$ai_latency": round(latency, 3) if latency is not None else None,
            "$ai_http_status": http_status,
            "$ai_is_error": bool(error),
            "$ai_temperature": temperature,
            "$ai_max_tokens": max_tokens,
            "$ai_stop_reason": stop_reason,
            "attempt": attempt,
            **{k: v for k, v in extra.items() if v is not None},
        }
        if error:
            props["$ai_error"] = scrub(str(error))[:2000]
        self.events.append({"event": "$ai_generation", "properties": props, "timestamp": _iso(time.time())})

    def finish(self, input_state=None, output_state=None, error=None, **extra):
        """Send the trace and its generations. Returns the HTTP status, or None if skipped or failed."""
        if not self.key:
            return None
        props = {
            **self._base(),
            "$ai_span_name": self.span_name,
            "$ai_input_state": scrub(input_state),
            "$ai_output_state": scrub(output_state),
            "$ai_latency": round(time.time() - self.started, 3),
            "$ai_is_error": bool(error),
            **{k: v for k, v in extra.items() if v is not None},
        }
        if error:
            props["$ai_error"] = scrub(str(error))[:2000]
        batch = self.events + [{"event": "$ai_trace", "properties": props, "timestamp": _iso(time.time())}]
        try:
            status = (self._post or _post)(f"{self.host}/batch/", {"api_key": self.key, "batch": batch}, self._timeout)
            log.info("posthog llm trace %s: %d events, http %s", self.trace_id, len(batch), status)
            return status
        except Exception as e:
            log.warning("posthog llm trace failed: %s", type(e).__name__)
            return None
