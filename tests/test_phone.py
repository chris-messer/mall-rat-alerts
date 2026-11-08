import unittest

from fakes import TEST_PHONE, install_stubs

install_stubs()

from common.phone import mask, normalize_us  # noqa: E402


class NormalizeTest(unittest.TestCase):
    def test_accepts_common_us_formats(self):
        for raw in ("(212) 555-2368", "212-555-2368", "212.555.2368", "2125552368",
                    "12125552368", "+1 212 555 2368", "+12125552368", " 212 555 2368 "):
            self.assertEqual(normalize_us(raw), "+12125552368", raw)

    def test_rejects_non_us_nanp(self):
        for raw in ("+14165552368",   # Toronto
                    "+18765552368",   # Jamaica
                    "+18095552368",   # Dominican Republic
                    "+14735552368",   # Grenada
                    "+16495552368"):  # Turks and Caicos
            self.assertIsNone(normalize_us(raw), raw)

    def test_rejects_toll_free_premium_and_personal_service(self):
        for raw in ("+18005552368", "+18335552368", "+18885552368", "+19005552368", "+15005552368"):
            self.assertIsNone(normalize_us(raw), raw)

    def test_rejects_n11_and_fictional_range(self):
        self.assertIsNone(normalize_us("+12124112368"))
        self.assertIsNone(normalize_us("+12125550123"))

    def test_rejects_bad_shapes(self):
        for raw in ("", "abc", "212555236", "212555236812", "+442071838750", "212-555-2368 ext 5",
                    "2125552368+", "++12125552368", "<script>", None, 2125552368, "1" * 40,
                    "+1 (012) 555-2368", "+1 (212) 155-2368"):
            self.assertIsNone(normalize_us(raw), repr(raw))

    def test_test_numbers_bypass_area_code_rules_only_when_listed(self):
        self.assertIsNone(normalize_us(TEST_PHONE))
        self.assertEqual(normalize_us(TEST_PHONE, {TEST_PHONE}), TEST_PHONE)
        self.assertEqual(normalize_us("(500) 555-0006", {TEST_PHONE}), TEST_PHONE)

    def test_mask(self):
        self.assertEqual(mask(TEST_PHONE), "+1******0006")
        self.assertEqual(mask(""), "***")


if __name__ == "__main__":
    unittest.main()
