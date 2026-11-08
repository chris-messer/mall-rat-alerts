import unittest

from fakes import install_stubs

install_stubs()

from common import turnstile  # noqa: E402

NOW = 1793952000.0
FRESH = "2026-11-06T07:59:00Z"


def fake(resp=None, exc=None):
    def post(url, data):
        if exc:
            raise exc
        return resp
    return post


class TurnstileTest(unittest.TestCase):
    def ok(self, **over):
        r = {"success": True, "hostname": "mallratalerts.com", "action": "subscribe", "challenge_ts": FRESH}
        r.update(over)
        return r

    def check(self, resp=None, exc=None, token="tok", secret="s"):
        return turnstile.verify(token, secret, "198.51.100.7", "subscribe", ["mallratalerts.com"],
                                post=fake(resp, exc), now=NOW)

    def test_success(self):
        self.assertTrue(self.check(self.ok()))

    def test_failures(self):
        self.assertFalse(self.check(self.ok(success=False)))
        self.assertFalse(self.check(self.ok(hostname="evil.example")))
        self.assertFalse(self.check(self.ok(action="unsubscribe")))
        self.assertFalse(self.check(self.ok(challenge_ts="2026-11-06T07:00:00Z")))
        self.assertFalse(self.check(self.ok(challenge_ts="garbage")))
        self.assertFalse(self.check(exc=OSError("network")))
        self.assertFalse(self.check(self.ok(), token=""))
        self.assertFalse(self.check(self.ok(), secret=""))


if __name__ == "__main__":
    unittest.main()
