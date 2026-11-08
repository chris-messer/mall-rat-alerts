"""Inbound SMS keyword classification and reply copy (GSM-7 only, no emoji).

Every reply is passed through common.sms_text.sanitize("public") by signup_api.twiml(), so even
copy edited live in DynamoDB can't turn a reply into UCS-2.
"""

import re

from common import sms_text

STOP_WORDS = {"STOP", "STOPALL", "UNSUBSCRIBE", "CANCEL", "END", "QUIT", "REVOKE", "OPTOUT"}
START_WORDS = {"START", "UNSTOP"}
HELP_WORDS = {"HELP", "INFO"}
YES_WORDS = {"YES", "Y", "YEP", "YEAH", "CONFIRM"}

# FCC revocation rule: honor opt-out "by any reasonable means", so short free text that
# clearly asks us to stop counts as STOP.
_STOPLIKE = re.compile(r"\b(stop|unsubscribe|opt ?out|revoke|cancel|quit|end)\b", re.I)
_STOPLIKE_MAX_LEN = 60

BRAND = "Mall Rat Alerts"


def classify(body):
    """Returns one of STOP, START, HELP, YES, OTHER. Tag matching (FOB ...) happens after this, on OTHER."""
    text = (body or "").strip()
    word = re.sub(r"[^A-Z ]", "", text.upper()).strip()
    compact = word.replace(" ", "")
    if compact in STOP_WORDS or word in ("OPT OUT", "STOP ALL"):
        return "STOP"
    if compact in START_WORDS:
        return "START"
    if compact in HELP_WORDS:
        return "HELP"
    if compact in YES_WORDS:
        return "YES"
    if len(text) <= _STOPLIKE_MAX_LEN and _STOPLIKE.search(text):
        return "STOP"
    return "OTHER"


def _site_host(url):
    return re.sub(r"^https?://(www\.)?", "", url or "").rstrip("/")


def _join_hint(tags):
    """'Text FOB to join Twilight Town Mall alerts' (one clause per tag)."""
    parts = [f"Text {t['tag']} to join {t.get('campaign_name') or t['tag']} alerts" for t in (tags or [])[:3]]
    return "; ".join(parts)


def welcome(campaign_name, source_site, waitlist_mode):
    when = " Alerts start when we launch." if waitlist_mode else ""
    return (f"{BRAND}: You're in for {campaign_name} alerts! We'll text when {_site_host(source_site)} changes."
            f"{when} Up to 6 msgs/day. Msg & data rates may apply. HELP for help, STOP to opt out. "
            "Unofficial fan project <3")


def tag_welcome(tag, waitlist_mode):
    """Welcome copy stored with the tag item, with the waitlist note added while broadcasts are off."""
    text = (tag.get("welcome_message") or "").strip()
    if not text:
        text = (f"{BRAND}: You're in for {tag.get('campaign_name') or tag['tag']} alerts.\n\n"
                "We'll text when the site changes. Msg freq varies. Msg&data rates may apply. "
                "Reply STOP to opt out, HELP for help. Unofficial fan project.")
    if waitlist_mode and "launch" not in text.lower():
        text += " Alerts start at launch."
    return text


def multi_welcome(names, waitlist_mode):
    when = " Alerts start at launch." if waitlist_mode else ""
    joined = ", ".join(names[:-1]) + f" and {names[-1]}" if len(names) > 1 else names[0]
    return (f"{BRAND}: You're in for {joined} alerts.\n\n"
            "We'll text when those sites change. Msg freq varies. Msg&data rates may apply. "
            f"Reply STOP to opt out, HELP for help. Unofficial fan project.{when}")


def already_active(campaign_name):
    return f"{BRAND}: You're already signed up for {campaign_name} alerts. HELP for help, STOP to opt out."


def help_text(support_email, tags=None):
    hint = _join_hint(tags)
    return (f"{BRAND}: Unofficial fan alerts, not affiliated with any artist. Up to 6 msgs/day. "
            f"Msg & data rates may apply. Help: {support_email}. " + (hint + ". " if hint else "")
            + "Reply STOP to opt out.")


def unknown_text(tags):
    hint = _join_hint(tags) or "Sign up on our website"
    return f"{BRAND}: Sorry, we didn't get that. {hint}. HELP for help, STOP to opt out. Unofficial fan project."


def start_text(site_url, tags=None):
    hint = _join_hint(tags)
    return (f"{BRAND}: You can get texts from this number again. To rejoin an alert campaign, "
            + (hint + " or sign up at " if hint else "sign up at ") + f"{_site_host(site_url)}. HELP for help, STOP to opt out.")


def no_signup(site_url, tags=None):
    hint = _join_hint(tags)
    return (f"{BRAND}: We couldn't find a signup waiting for this number. "
            + (hint + " or join at " if hint else "Join at ") + f"{_site_host(site_url)}. HELP for help, STOP to opt out.")


def is_gsm7(text):
    return sms_text.is_gsm7(text)


def segments(text):
    return sms_text.segments(text)
