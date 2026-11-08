"""Text-to-join: tag matching, subscribe flow, welcome copy, router order."""

import re
import unittest

from apibase import CAMPAIGN, OTHER_CAMPAIGN, FOB_TAG, NEW_TAG
from fakes import TEST_PHONE
from smsbase import EMOJI, SmsTestCase

from common import sms_text, tags  # noqa: E402

TAGS = [FOB_TAG, NEW_TAG]


class MatchTest(unittest.TestCase):
    def ids(self, body, tag_list=TAGS):
        return [t["tag"] for t in tags.match(body, tag_list)]

    def test_variants_match(self):
        for body in ("FOB", "fob", "  FOB  ", "FOB!", "f.o.b", "Fob.", "Fall Out Boy", "fall   out   boy",
                     "FALLOUTBOY", "fallout boy", "join fob", "\tfob\n"):
            self.assertEqual(self.ids(body), ["FOB"], body)

    def test_multiple_tags_and_extra_words(self):
        self.assertEqual(self.ids("FOB NEWTAG"), ["FOB", "NEWTAG"])
        self.assertEqual(self.ids("fob please future"), ["FOB", "NEWTAG"])
        self.assertEqual(self.ids("fob FALLOUTBOY"), ["FOB"])  # same campaign once

    def test_non_matches(self):
        for body in ("", "hello", "forbidden", "who is this", "x" * 80 + " fob", "a b c d e f g h i fob"):
            self.assertEqual(self.ids(body), [], body)

    def test_disabled_tag_ignored(self):
        off = dict(FOB_TAG, enabled=False)
        self.assertEqual(self.ids("FOB", [off]), [])

    def test_default_tags_work_without_table(self):
        self.assertEqual([t["tag"] for t in tags.match("FOB", tags.DEFAULT_TAGS)], ["FOB"])


class SmsSubscribeTest(SmsTestCase):
    def sub(self, campaign=CAMPAIGN):
        return self.subs.items.get((TEST_PHONE, campaign))

    def test_fob_subscribes_active_with_welcome(self):
        res = self.sms("FOB")
        msg = self.message(res)
        sub = self.sub()
        self.assertEqual((sub["status"], sub["source"], sub["tier"], sub["sms_tag"]), ("ACTIVE", "sms", "PUBLIC", "FOB"))
        self.assertEqual(sub["gsi1pk"], f"ACTIVE#{CAMPAIGN}")
        self.assertEqual(sub["consent_version"], "sms-keyword-v1")
        self.assertEqual(sub["campaign_name"], "Twilight Town Mall")
        self.assertEqual(sub["source_site"], "https://www.falloutboy.com")
        self.assertEqual(sub["campaign_tags"], ["fob", "twilight-town-mall", "2026"])
        self.assertTrue(msg.startswith("Mall Rat Alerts: You're in for Twilight Town Mall (Fall Out Boy site alerts)."))
        self.assertIn("\n\n", msg)
        for phrase in ("STOP", "HELP", "Unofficial fan project", "Msg&data rates may apply"):
            self.assertIn(phrase, msg)
        self.assertTrue(sms_text.is_gsm7(msg))
        self.assertIsNone(EMOJI.search(msg))
        self.assertLessEqual(sms_text.segments(msg), 2)

    def test_waitlist_note_added_while_broadcasts_off(self):
        self.assertIn("Alerts start at launch", self.message(self.sms("FOB")))

    def test_audit_record(self):
        self.sms("fob!")
        (a,) = [i for i in self.audit.items.values() if i["event_type"] == "subscribe_sms_tag"]
        self.assertEqual((a["tag"], a["campaign_id"], a["consent_version"], a["new_status"], a["source"]),
                         ("FOB", CAMPAIGN, "sms-keyword-v1", "ACTIVE", "sms"))
        self.assertIn("consent_captured_at", a)
        self.assertEqual(a["message_sid"], "SMtest123")

    def test_every_variant_subscribes(self):
        for body in ("fob", "Fall Out Boy", "FALLOUTBOY", " FOB! "):
            self.subs.items.clear()
            self.assertIn("You're in for Twilight Town Mall", self.message(self.sms(body)), body)
            self.assertEqual(self.sub()["status"], "ACTIVE")

    def test_already_active_replies_once_a_day(self):
        self.sms("FOB")
        self.audit.items.clear()
        first = self.message(self.sms("FOB"))
        self.assertIn("already signed up", first)
        self.assertIsNone(self.message(self.sms("FOB")))
        self.assertEqual(self.sub()["status"], "ACTIVE")
        self.assertNotIn("subscribe_sms_tag", self.audit_events())
        self.assertIn("sms_tag_already_active", self.audit_events())

    def test_opted_out_rejoins_and_is_audited(self):
        self.member(status="OPTED_OUT", unsubscribed_at="2026-11-01T00:00:00.000Z", unsubscribe_method="STOP_KEYWORD")
        msg = self.message(self.sms("FOB"))
        self.assertIn("You're in", msg)
        sub = self.sub()
        self.assertEqual((sub["status"], sub["source"]), ("ACTIVE", "sms"))
        self.assertEqual(sub["previous_unsubscribed_at"], "2026-11-01T00:00:00.000Z")
        (a,) = self.audit.items.values()
        self.assertEqual((a["event_type"], a["prior_status"]), ("subscribe_sms_tag", "OPTED_OUT"))

    def test_pending_web_signup_promoted(self):
        self.member(status="PENDING_SENDER", source="web", expires_at=1)
        self.assertIn("You're in", self.message(self.sms("FOB")))
        sub = self.sub()
        self.assertEqual((sub["status"], sub["source"], sub["activation_source"]), ("ACTIVE", "web", "sms_tag"))
        self.assertNotIn("expires_at", sub)
        self.assertEqual(sub["gsi1pk"], f"ACTIVE#{CAMPAIGN}")

    def test_blocked_stays_blocked(self):
        self.member(status="BLOCKED")
        self.assertIsNone(self.message(self.sms("FOB")))
        self.assertEqual(self.sub()["status"], "BLOCKED")

    def test_multiple_tags_one_combined_welcome(self):
        msg = self.message(self.sms("FOB NEWTAG"))
        self.assertIn("Twilight Town Mall and Future", msg)
        self.assertEqual(self.sub()["status"], "ACTIVE")
        self.assertEqual(self.sub(OTHER_CAMPAIGN)["status"], "ACTIVE")
        self.assertEqual(sorted(self.audit_events()), ["subscribe_sms_tag", "subscribe_sms_tag"])

    def test_extra_words_ignored_when_tag_matches(self):
        self.assertIn("You're in", self.message(self.sms("hey fob thanks")))

    def test_unknown_tag_with_one_known_subscribes_to_known(self):
        self.assertIn("You're in for Twilight Town Mall", self.message(self.sms("FOB BOGUS")))

    def test_unknown_body_lists_tags_once_a_day(self):
        msg = self.message(self.sms("what is this"))
        self.assertIn("Text FOB to join Twilight Town Mall alerts", msg)
        self.assertTrue(sms_text.is_gsm7(msg))
        self.assertIsNone(EMOJI.search(msg))
        self.assertIsNone(self.message(self.sms("hello again")))
        self.assertIsNone(self.sub())

    def test_stop_help_yes_start_not_hijacked(self):
        self.member(status="ACTIVE")
        self.assertIsNone(self.message(self.sms("STOP")))
        self.assertEqual(self.sub()["status"], "OPTED_OUT")
        self.assertIn("rejoin", self.message(self.sms("START")))
        self.assertIn("Text FOB to join", self.message(self.sms("HELP")))
        self.assertIn("couldn't find a signup", self.message(self.sms("YES")))
        self.assertEqual(self.sub()["status"], "OPTED_OUT")

    def test_stop_phrases_containing_tag_still_stop(self):
        self.member(status="ACTIVE")
        self.sms("STOP FOB")
        self.assertEqual(self.sub()["status"], "OPTED_OUT")

    def test_help_text_mentions_fob_and_is_gsm7(self):
        msg = self.message(self.sms("HELP"))
        self.assertIn("Text FOB to join Twilight Town Mall alerts", msg)
        self.assertIn("not affiliated", msg)
        self.assertTrue(sms_text.is_gsm7(msg))

    def test_yes_confirmation_still_works(self):
        self.member(status="PENDING_SENDER")
        self.assertIn("You're in", self.message(self.sms("YES")))
        self.assertEqual(self.sub()["status"], "ACTIVE")

    def test_reply_is_sanitized_even_if_welcome_copy_has_smart_punctuation(self):
        self.templates.put_item(Item=dict(FOB_TAG, welcome_message=
            "Mall Rat Alerts: You\u2019re in \u2014 \u201cTwilight Town Mall\u201d\u2026 \U0001F496 Reply STOP to opt out."))
        from common import store
        store.reset_config_cache()
        msg = self.message(self.sms("FOB"))
        self.assertIn("You're in - \"Twilight Town Mall\"...", msg)
        self.assertTrue(sms_text.is_gsm7(msg))
        self.assertNotIn("\U0001F496", msg)

    def test_welcome_copy_edit_applies_without_redeploy(self):
        from common import store
        self.templates.put_item(Item=dict(FOB_TAG, welcome_message="Mall Rat Alerts: custom welcome. Reply STOP to opt out."))
        store.reset_config_cache()
        self.assertIn("custom welcome", self.message(self.sms("FOB")))

    def test_new_tag_from_table_works(self):
        msg = self.message(self.sms("future"))
        self.assertIn("Future alerts", msg)
        self.assertEqual(self.sub(OTHER_CAMPAIGN)["status"], "ACTIVE")

    def test_unregistered_campaign_is_skipped(self):
        self.templates.put_item(Item=dict(NEW_TAG, campaign_id="not-registered-2030"))
        from common import store
        store.reset_config_cache()
        self.assertIsNone(self.message(self.sms("NEWTAG")))
        self.assertEqual(self.subs.items, {})

    def test_falls_back_to_default_tags_if_table_empty(self):
        self.templates.items.clear()
        from common import store
        store.reset_config_cache()
        self.assertIn("You're in", self.message(self.sms("FOB")))

    def test_posthog_event_has_no_phone(self):
        self.sms("FOB")
        events = [e for e in self.tracked if e[0] == "sms_subscribe"]
        self.assertEqual(len(events), 1)
        _, distinct, props = events[0]
        self.assertEqual((props["tag"], props["campaign_id"], props["source"]), ("FOB", CAMPAIGN, "sms"))
        blob = repr(events)
        self.assertNotIn(TEST_PHONE, blob)
        self.assertNotIn("5005550006", blob)
        self.assertTrue(distinct.startswith("srv_"))

    def test_twilio_status_callback_is_not_a_subscribe(self):
        res = self.sms("FOB", path="/api/twilio/status", extra={"MessageStatus": "delivered", "To": TEST_PHONE})
        self.assertEqual(res["statusCode"], 204)
        self.assertIsNone(self.sub())


if __name__ == "__main__":
    unittest.main()
