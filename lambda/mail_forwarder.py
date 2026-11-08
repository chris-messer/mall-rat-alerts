"""Forward mail received by SES inbound for hello@mallratalerts.com to a personal inbox.

Flow: SES receipt rule stores the raw message in S3 (prefix hello/), then invokes this function
asynchronously. We read the raw message, rewrite a few headers, and send it with SES SendRawEmail.

Header changes only; the body bytes are passed through untouched:
  From      -> hello@mallratalerts.com (the verified sending identity)
  Reply-To  -> the original Reply-To, else the original From (so replies reach the sender)
  Subject   -> original subject, prefixed with "[hello@] " unless already prefixed
  removed   -> Return-Path, Sender, DKIM-Signature, DomainKey-Signature (invalid after the From rewrite)
  added     -> X-Mall-Rat-Forwarded (loop marker)

Loop and noise guards (message is dropped, not forwarded):
  - Auto-Submitted header present with any value other than "no"
  - null Return-Path ("<>"), i.e. bounces. SES stamps a Return-Path header on every stored message,
    so the mere presence of the header cannot be the test; an empty one can.
  - our own X-Mall-Rat-Forwarded marker, or the envelope sender is on our own domain
  - SES virus verdict FAIL

Logs carry only the SES message ID and the sender domain, never addresses or bodies.
Env: INBOUND_BUCKET, KEY_PREFIX (default "hello/"), FORWARD_TO, FROM_ADDRESS, DRY_RUN.
"""

import email
import email.policy
import email.utils
import logging
import os
import re
from email.header import Header, decode_header, make_header

logger = logging.getLogger()
logger.setLevel(logging.INFO)

SUBJECT_PREFIX = "[hello@] "
MARKER = "X-Mall-Rat-Forwarded"
STRIP_HEADERS = ("Return-Path", "Sender", "DKIM-Signature", "DomainKey-Signature", "From", "Reply-To", "Subject")

_clients = {}


def _client(name):
    if name not in _clients:
        import boto3  # imported lazily so unit tests need no boto3

        _clients[name] = boto3.client(name)
    return _clients[name]


def _cfg(name, default=""):
    return os.environ.get(name, default)


def _dry_run():
    return _cfg("DRY_RUN", "false").strip().lower() == "true"


def _split(raw):
    m = re.search(rb"\r?\n\r?\n", raw)
    if not m:
        return raw, b"", b"\r\n\r\n"
    return raw[: m.start()], raw[m.end():], m.group(0)


def _sender_domain(addr):
    _, a = email.utils.parseaddr(addr or "")
    return a.rsplit("@", 1)[-1].lower() if "@" in a else "unknown"


def drop_reason(msg, own_domain, envelope_source="", virus_verdict=""):
    """Return a short reason string if the message must not be forwarded, else None."""
    auto = (msg.get("Auto-Submitted") or "").strip().lower()
    if auto and auto != "no":
        return "auto-submitted"
    rp = msg.get("Return-Path")
    if rp is not None and rp.strip() in ("<>", ""):
        return "null-return-path"
    if msg.get(MARKER) is not None:
        return "already-forwarded"
    if own_domain and _sender_domain(envelope_source) == own_domain.lower():
        return "own-domain-sender"
    if (virus_verdict or "").upper() == "FAIL":
        return "virus"
    return None


def _decoded_subject(msg):
    raw = msg.get("Subject")
    if raw is None:
        return ""
    try:
        return str(make_header(decode_header(str(raw))))
    except Exception:  # malformed encoded-word: keep the raw text
        return str(raw)


def rewrite(raw, from_address, forward_marker="1"):
    """Return the new raw message bytes. Only the header block is rebuilt."""
    head, body, sep = _split(raw)
    pol = email.policy.compat32.clone(linesep="\r\n")
    msg = email.message_from_bytes(head + b"\r\n\r\n", policy=pol)

    original_from = msg.get("From", "")
    reply_to = msg.get("Reply-To") or original_from
    subject = _decoded_subject(msg)
    if not subject.startswith(SUBJECT_PREFIX):
        subject = SUBJECT_PREFIX + subject

    for name in STRIP_HEADERS:
        del msg[name]
    msg["From"] = from_address
    if reply_to:
        msg["Reply-To"] = str(reply_to)
    try:
        subject.encode("ascii")
        msg["Subject"] = subject
    except UnicodeEncodeError:
        msg["Subject"] = Header(subject, "utf-8")
    msg[MARKER] = forward_marker

    new_head = msg.as_bytes(policy=pol)
    # as_bytes ends the header block with the blank line; body is appended unchanged.
    if not new_head.endswith(b"\r\n\r\n"):
        new_head = new_head.rstrip(b"\r\n") + b"\r\n\r\n"
    return new_head + body


def _record(event):
    rec = event["Records"][0]["ses"]
    return rec["mail"], rec.get("receipt", {})


def handler(event, context=None):
    mail, receipt = _record(event)
    message_id = mail["messageId"]
    bucket = _cfg("INBOUND_BUCKET")
    key = _cfg("KEY_PREFIX", "hello/") + message_id
    own_domain = _sender_domain(_cfg("FROM_ADDRESS", "hello@mallratalerts.com"))
    forward_to = _cfg("FORWARD_TO", "chrislmesser@gmail.com")
    from_address = _cfg("FROM_ADDRESS", "hello@mallratalerts.com")

    raw = _client("s3").get_object(Bucket=bucket, Key=key)["Body"].read()
    head, _, _ = _split(raw)
    parsed = email.message_from_bytes(head + b"\r\n\r\n", policy=email.policy.compat32)
    source = mail.get("source", "")
    sdomain = _sender_domain(source)

    reason = drop_reason(parsed, own_domain, source, (receipt.get("virusVerdict") or {}).get("status", ""))
    if reason:
        logger.info("dropped message_id=%s sender_domain=%s reason=%s", message_id, sdomain, reason)
        return {"status": "dropped", "reason": reason}

    new_raw = rewrite(raw, from_address)
    if _dry_run():
        logger.info("dry-run message_id=%s sender_domain=%s bytes=%d", message_id, sdomain, len(new_raw))
        return {"status": "dry-run", "bytes": len(new_raw)}

    _client("ses").send_raw_email(
        Source=from_address,
        Destinations=[forward_to],
        RawMessage={"Data": new_raw},
    )
    logger.info("forwarded message_id=%s sender_domain=%s bytes=%d", message_id, sdomain, len(new_raw))
    return {"status": "forwarded"}
