"""Mail forwarder tests. Fake addresses only (example.com / example.net); nothing is sent."""

import email
import io
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lambda"))

import mail_forwarder as mf  # noqa: E402

BODY = b"Hello there,\r\n\r\nThis body must survive unchanged.\r\n.dot line\r\nFrom the desk\r\n"


def raw_msg(extra=b"", subject=b"Question about alerts", from_=b"Pat Example <pat@example.net>"):
    return (
        b"Return-Path: <pat@example.net>\r\n"
        b"Received: from mail.example.net by inbound-smtp.us-east-1.amazonaws.com\r\n"
        b"DKIM-Signature: v=1; a=rsa-sha256; d=example.net; s=x; b=abc\r\n"
        b"From: " + from_ + b"\r\n"
        b"To: hello@mallratalerts.com\r\n"
        b"Subject: " + subject + b"\r\n"
        b"Message-ID: <abc@example.net>\r\n" + extra + b"\r\n" + BODY
    )


class FakeBody:
    def __init__(self, data):
        self._d = data

    def read(self):
        return self._d


class FakeS3:
    def __init__(self, data):
        self.data, self.requests = data, []

    def get_object(self, Bucket, Key):
        self.requests.append((Bucket, Key))
        return {"Body": FakeBody(self.data)}


class FakeSes:
    def __init__(self):
        self.sent = []

    def send_raw_email(self, **kw):
        self.sent.append(kw)
        return {"MessageId": "x"}


def ses_event(message_id="msgid123", source="pat@example.net", virus="PASS"):
    return {"Records": [{"ses": {
        "mail": {"messageId": message_id, "source": source},
        "receipt": {"virusVerdict": {"status": virus}, "spamVerdict": {"status": "PASS"}},
    }}]}


def parse(raw):
    return email.message_from_bytes(raw)


class RewriteTests(unittest.TestCase):
    def test_header_rewrite(self):
        out = mf.rewrite(raw_msg(), "hello@mallratalerts.com")
        m = parse(out)
        self.assertEqual(m["From"], "hello@mallratalerts.com")
        self.assertEqual(m["Reply-To"], "Pat Example <pat@example.net>")
        self.assertEqual(m["Subject"], "[hello@] Question about alerts")
        self.assertIsNone(m["Return-Path"])
        self.assertIsNone(m["DKIM-Signature"])
        self.assertEqual(m["Message-ID"], "<abc@example.net>")
        self.assertEqual(m["To"], "hello@mallratalerts.com")
        self.assertEqual(m[mf.MARKER], "1")

    def test_body_unchanged_byte_for_byte(self):
        out = mf.rewrite(raw_msg(), "hello@mallratalerts.com")
        self.assertTrue(out.endswith(b"\r\n\r\n" + BODY))
        self.assertEqual(out.count(BODY), 1)

    def test_existing_reply_to_is_kept(self):
        out = mf.rewrite(raw_msg(extra=b"Reply-To: team@example.org\r\n"), "hello@mallratalerts.com")
        self.assertEqual(parse(out)["Reply-To"], "team@example.org")

    def test_subject_not_double_prefixed(self):
        out = mf.rewrite(raw_msg(subject=b"[hello@] Already tagged"), "hello@mallratalerts.com")
        self.assertEqual(parse(out)["Subject"], "[hello@] Already tagged")

    def test_encoded_subject_is_decoded_then_prefixed(self):
        out = mf.rewrite(raw_msg(subject=b"=?utf-8?q?Caf=C3=A9_hours?="), "hello@mallratalerts.com")
        subj = str(email.header.make_header(email.header.decode_header(parse(out)["Subject"])))
        self.assertEqual(subj, "[hello@] Caf\u00e9 hours")

    def test_missing_subject_gets_prefix(self):
        raw = raw_msg().replace(b"Subject: Question about alerts\r\n", b"")
        self.assertEqual(parse(mf.rewrite(raw, "hello@mallratalerts.com"))["Subject"].strip(), "[hello@]")


class DropTests(unittest.TestCase):
    def reason(self, extra=b"", source="pat@example.net", virus="PASS", raw=None):
        m = parse(raw or raw_msg(extra))
        return mf.drop_reason(m, "mallratalerts.com", source, virus)

    def test_normal_mail_passes_even_with_return_path(self):
        self.assertIsNone(self.reason())

    def test_auto_submitted_dropped(self):
        self.assertEqual(self.reason(b"Auto-Submitted: auto-replied\r\n"), "auto-submitted")

    def test_auto_submitted_no_is_allowed(self):
        self.assertIsNone(self.reason(b"Auto-Submitted: no\r\n"))

    def test_null_return_path_dropped(self):
        raw = raw_msg().replace(b"Return-Path: <pat@example.net>", b"Return-Path: <>")
        self.assertEqual(self.reason(raw=raw), "null-return-path")

    def test_own_marker_dropped(self):
        self.assertEqual(self.reason(b"X-Mall-Rat-Forwarded: 1\r\n"), "already-forwarded")

    def test_own_domain_sender_dropped(self):
        self.assertEqual(self.reason(source="hello@mallratalerts.com"), "own-domain-sender")

    def test_virus_dropped(self):
        self.assertEqual(self.reason(virus="FAIL"), "virus")


class HandlerTests(unittest.TestCase):
    def setUp(self):
        os.environ.update({"INBOUND_BUCKET": "bucket-test", "KEY_PREFIX": "hello/",
                           "FROM_ADDRESS": "hello@mallratalerts.com"})
        os.environ.pop("FORWARD_TO", None)
        os.environ.pop("DRY_RUN", None)
        self.ses = FakeSes()
        mf._clients.clear()

    def use(self, data):
        self.s3 = FakeS3(data)
        mf._clients.update(s3=self.s3, ses=self.ses)

    def test_forwards_to_default_destination(self):
        self.use(raw_msg())
        res = mf.handler(ses_event())
        self.assertEqual(res["status"], "forwarded")
        self.assertEqual(self.s3.requests, [("bucket-test", "hello/msgid123")])
        sent = self.ses.sent[0]
        self.assertEqual(sent["Destinations"], ["chrislmesser@gmail.com"])
        self.assertEqual(sent["Source"], "hello@mallratalerts.com")
        self.assertEqual(parse(sent["RawMessage"]["Data"])["Subject"], "[hello@] Question about alerts")

    def test_forward_to_env_override(self):
        os.environ["FORWARD_TO"] = "someone@example.com"
        self.use(raw_msg())
        mf.handler(ses_event())
        self.assertEqual(self.ses.sent[0]["Destinations"], ["someone@example.com"])

    def test_dry_run_sends_nothing(self):
        os.environ["DRY_RUN"] = "true"
        self.use(raw_msg())
        res = mf.handler(ses_event())
        self.assertEqual(res["status"], "dry-run")
        self.assertEqual(self.ses.sent, [])

    def test_loop_message_is_dropped_not_sent(self):
        self.use(raw_msg(b"Auto-Submitted: auto-generated\r\n"))
        self.assertEqual(mf.handler(ses_event())["status"], "dropped")
        self.assertEqual(self.ses.sent, [])

    def test_logs_contain_no_addresses_or_body(self):
        self.use(raw_msg())
        with self.assertLogs(level="INFO") as cm:
            mf.handler(ses_event())
        text = "\n".join(cm.output)
        self.assertIn("msgid123", text)
        self.assertIn("example.net", text)
        self.assertNotIn("pat@", text)
        self.assertNotIn("This body", text)
        self.assertNotIn("chrislmesser", text)


if __name__ == "__main__":
    unittest.main()
