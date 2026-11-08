"""Sanitizer and segment math (same cases run in fob-site-monitor/tests)."""

import unittest

from fakes import install_stubs

install_stubs()

from common import sms_text  # noqa: E402


class SanitizeTest(unittest.TestCase):
    def test_dashes_become_hyphens(self):
        self.assertEqual(sms_text.sanitize("a\u2014b \u2013 c \u2212 d", "public"), "a-b - c - d")

    def test_curly_quotes(self):
        self.assertEqual(sms_text.sanitize("\u2018hi\u2019 \u201cthere\u201d it\u2019s", "public"), "'hi' \"there\" it's")

    def test_ellipsis_bullet_spaces_and_marks(self):
        self.assertEqual(sms_text.sanitize("wait\u2026 \u2022 go\u00a0now\u2009ok\u200b!", "public"), "wait... - go now ok!")
        self.assertEqual(sms_text.sanitize("Brand\u2122 \u00ae \u00a9 x", "public"), "Brand x")

    def test_nfkd_folds_accents_and_fullwidth(self):
        self.assertEqual(sms_text.sanitize("caf\u00e9 \u00e7a r\u00e9sum\u00e9 \uff21\uff22", "public"), "caf\u00e9 ca r\u00e9sum\u00e9 AB")
        self.assertEqual(sms_text.sanitize("na\u00efve \u0161", "public"), "naive s")

    def test_public_drops_emoji_and_stays_gsm7(self):
        raw = "New page \U0001F496 is live \u2014 go look \u2728\ufe0f\u200d \u201cnow\u201d\u2026"
        out = sms_text.sanitize(raw, "public")
        self.assertEqual(out, 'New page is live - go look "now"...')
        self.assertTrue(sms_text.is_gsm7(out))
        self.assertEqual(sms_text.stats(out)["encoding"], "GSM-7")

    def test_private_keeps_emoji_but_fixes_punctuation(self):
        out = sms_text.sanitize("Omg \U0001F496 it\u2019s open \u2014 yay\u2026", "private")
        self.assertEqual(out, "Omg \U0001F496 it's open - yay...")
        self.assertEqual(sms_text.encoding_of(out), "UCS-2")

    def test_keeps_newlines_and_collapses_spaces(self):
        self.assertEqual(sms_text.sanitize("a  b\n\n\n\nc \n d", "public"), "a b\n\nc\nd")

    def test_bad_mode(self):
        with self.assertRaises(ValueError):
            sms_text.sanitize("x", "nope")


class SegmentTest(unittest.TestCase):
    def test_gsm7_boundaries(self):
        self.assertEqual(sms_text.segments("a" * 160), 1)
        self.assertEqual(sms_text.segments("a" * 161), 2)
        self.assertEqual(sms_text.segments("a" * 306), 2)
        self.assertEqual(sms_text.segments("a" * 307), 3)
        self.assertEqual(sms_text.segments(""), 1)

    def test_gsm7_extension_chars_count_double(self):
        self.assertEqual(sms_text.units("[]"), 4)
        self.assertEqual(sms_text.segments("[" * 80), 1)
        self.assertEqual(sms_text.segments("[" * 81), 2)

    def test_ucs2_boundaries(self):
        self.assertEqual(sms_text.encoding_of("a\u2764"), "UCS-2")
        self.assertEqual(sms_text.segments("\u00e7" * 3 + "\u2764" + "a" * 66), 1)   # 70 units
        self.assertEqual(sms_text.segments("\u2764" + "a" * 70), 2)                   # 71 units
        self.assertEqual(sms_text.segments("\u2764" + "a" * 133), 2)                  # 134 units
        self.assertEqual(sms_text.segments("\u2764" + "a" * 134), 3)                  # 135 units

    def test_emoji_outside_bmp_counts_two_units(self):
        self.assertEqual(sms_text.units("\U0001F496"), 2)
        self.assertEqual(sms_text.segments("\U0001F496" * 35), 1)
        self.assertEqual(sms_text.segments("\U0001F496" * 36), 2)

    def test_stats(self):
        s = sms_text.stats("hello")
        self.assertEqual((s["segments"], s["encoding"], s["units"]), (1, "GSM-7", 5))


if __name__ == "__main__":
    unittest.main()
