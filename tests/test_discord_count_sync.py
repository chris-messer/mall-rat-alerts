"""Scheduled Discord count pull: mocked HTTP + DynamoDB for success, 401, 429, missing guild id."""

import io
import json
import unittest
from unittest import mock
import urllib.error

from fakes import FakeTable, install_stubs

install_stubs()

import discord_count_sync  # noqa: E402
from common import counts  # noqa: E402

CAMPAIGN = "fob-twilight-town-mall-2026"
GUILD = "1569100999606538352"
PK = counts.counts_pk(CAMPAIGN)
KEY = (PK, "current")
TOKEN = "unit-test-bot-token"


class DiscordHttp:
    """Minimal urlopen stand-in. body is the JSON payload or an exception to raise."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = 0

    def __call__(self, req, timeout=None):
        self.calls += 1
        self.last_req = req
        item = self.responses.pop(0) if self.responses else self.responses
        if isinstance(item, Exception):
            raise item
        raw = json.dumps(item).encode("utf-8")
        return io.BytesIO(raw)


def http_error(code, body=b"{}", headers=None):
    return urllib.error.HTTPError(
        url="https://discord.com/api/v10/guilds/x",
        code=code,
        msg="err",
        hdrs=headers or {},
        fp=io.BytesIO(body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")),
    )


class DiscordCountSyncTest(unittest.TestCase):
    def setUp(self):
        self.ops = FakeTable("pk", "sk")
        self.ops.put_item(Item={"pk": PK, "sk": "current", "sms_active": 119, "sms_opted_out": 2,
                                "discord_members": 1})
        self._saved = (
            discord_count_sync.DISCORD_GUILD_ID,
            discord_count_sync.CAMPAIGN_ID,
            discord_count_sync.OPS_TABLE,
            discord_count_sync._token_cache["value"],
            discord_count_sync._ops_table["value"],
        )
        discord_count_sync.DISCORD_GUILD_ID = GUILD
        discord_count_sync.CAMPAIGN_ID = CAMPAIGN
        discord_count_sync.OPS_TABLE = "ops"
        discord_count_sync._token_cache["value"] = TOKEN
        discord_count_sync._ops_table["value"] = self.ops

    def tearDown(self):
        (discord_count_sync.DISCORD_GUILD_ID, discord_count_sync.CAMPAIGN_ID,
         discord_count_sync.OPS_TABLE, discord_count_sync._token_cache["value"],
         discord_count_sync._ops_table["value"]) = self._saved

    def test_success_sets_members_presence_and_leaves_sms(self):
        http = DiscordHttp({"approximate_member_count": 27, "approximate_presence_count": 4})
        with mock.patch("urllib.request.urlopen", http):
            out = discord_count_sync.sync_once(ops_table=self.ops, token=TOKEN)
        self.assertEqual(out["members"], 27)
        self.assertEqual(out["presence"], 4)
        self.assertEqual(out["result"], "ok")
        item = self.ops.items[KEY]
        self.assertEqual(item["discord_members"], 27)
        self.assertEqual(item["discord_presence"], 4)
        self.assertIn("discord_synced_at", item)
        self.assertEqual(item["sms_active"], 119)
        self.assertEqual(item["sms_opted_out"], 2)
        self.assertEqual(http.calls, 1)
        self.assertIn("with_counts=true", http.last_req.full_url)
        self.assertTrue(http.last_req.get_header("Authorization").startswith("Bot "))

    def test_401_fails_without_retry(self):
        http = DiscordHttp(http_error(401))
        with mock.patch("urllib.request.urlopen", http):
            with self.assertRaises(urllib.error.HTTPError) as cm:
                discord_count_sync.sync_once(ops_table=self.ops, token=TOKEN)
        self.assertEqual(cm.exception.code, 401)
        self.assertEqual(http.calls, 1)
        self.assertEqual(self.ops.items[KEY]["discord_members"], 1)

    def test_429_retries_then_succeeds(self):
        http = DiscordHttp(
            http_error(429, {"retry_after": 0.01}),
            {"approximate_member_count": 30, "approximate_presence_count": 2},
        )
        with mock.patch("urllib.request.urlopen", http), mock.patch("time.sleep"):
            out = discord_count_sync.sync_once(ops_table=self.ops, token=TOKEN)
        self.assertEqual(out["members"], 30)
        self.assertEqual(http.calls, 2)
        self.assertEqual(self.ops.items[KEY]["discord_members"], 30)

    def test_429_exhausted_raises(self):
        http = DiscordHttp(http_error(429, {"retry_after": 0.01}),
                           http_error(429, {"retry_after": 0.01}),
                           http_error(429, {"retry_after": 0.01}))
        with mock.patch("urllib.request.urlopen", http), mock.patch("time.sleep"):
            with self.assertRaises(urllib.error.HTTPError) as cm:
                discord_count_sync.sync_once(ops_table=self.ops, token=TOKEN)
        self.assertEqual(cm.exception.code, 429)
        self.assertEqual(http.calls, 3)
        self.assertEqual(self.ops.items[KEY]["discord_members"], 1)

    def test_missing_guild_id_raises(self):
        discord_count_sync.DISCORD_GUILD_ID = ""
        with self.assertRaisesRegex(RuntimeError, "DISCORD_GUILD_ID"):
            discord_count_sync.sync_once(ops_table=self.ops, token=TOKEN, guild_id="")
        self.assertEqual(self.ops.items[KEY]["discord_members"], 1)


if __name__ == "__main__":
    unittest.main()
