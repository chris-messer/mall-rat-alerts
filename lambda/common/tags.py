"""Text-to-join tags: a tag (FOB) maps to a campaign. Items live in the templates table so they
can be edited without a deploy:

    pk = "config#tags", sk = "<TAG>"   {tag, aliases[], campaign_id, campaign_name, welcome_message, enabled}

Matching is case-insensitive and alphanumeric only ("FOB!", "f.o.b", "Fall Out Boy" and
"FALLOUTBOY" all match). Up to three adjacent words are glued together to match multi-word aliases.
If no table item can be read, DEFAULT_TAGS keeps the service working.
"""

import re

MAX_BODY_CHARS = 60
MAX_TOKENS = 8
CONSENT_VERSION = "sms-keyword-v1"

DEFAULT_WELCOME = (
    "Mall Rat Alerts: You're in for Twilight Town Mall (Fall Out Boy site alerts).\n\n"
    "We'll text when the site changes. Msg freq varies. Msg&data rates may apply. "
    "Reply STOP to opt out, HELP for help. Unofficial fan project."
)

DEFAULT_TAGS = [{
    "tag": "FOB",
    "aliases": ["FOB", "FALLOUTBOY"],
    "campaign_id": "fob-twilight-town-mall-2026",
    "campaign_name": "Twilight Town Mall",
    "welcome_message": DEFAULT_WELCOME,
    "enabled": True,
}]


def norm(text):
    return re.sub(r"[^A-Z0-9]", "", (text or "").upper())


def alias_map(tags):
    out = {}
    for t in tags:
        if t.get("enabled", True) is False:
            continue
        for a in [t.get("tag", "")] + list(t.get("aliases") or []):
            key = norm(a)
            if key:
                out.setdefault(key, t)
    return out


def match(body, tags):
    """Tag items named in `body`, in order, one per campaign. Empty if the text is too long to be a keyword."""
    text = (body or "").strip()
    if not text or len(text) > MAX_BODY_CHARS:
        return []
    words = [w for w in (norm(w) for w in text.split()) if w]
    if not words or len(words) > MAX_TOKENS:
        return []
    aliases = alias_map(tags)
    found, seen, i = [], set(), 0
    while i < len(words):
        for n in (3, 2, 1):
            tag = aliases.get("".join(words[i:i + n])) if i + n <= len(words) else None
            if tag:
                if tag["campaign_id"] not in seen:
                    seen.add(tag["campaign_id"])
                    found.append(tag)
                i += n
                break
        else:
            i += 1
    return found
