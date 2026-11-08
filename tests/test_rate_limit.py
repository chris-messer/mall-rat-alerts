import unittest

from fakes import FakeTable, install_stubs

install_stubs()

from common import store  # noqa: E402

NOW = 1793952000.0  # 2026-11-06T08:00:00Z


class RateLimitTest(unittest.TestCase):
    def setUp(self):
        self.ops = FakeTable("pk", "sk")

    def test_allows_up_to_limit_then_blocks(self):
        results = [store.hit(self.ops, "rl#ip#c#h", "H", 3, NOW) for _ in range(5)]
        self.assertEqual(results, [True, True, True, False, False])

    def test_counter_has_ttl_after_window_end(self):
        store.hit(self.ops, "rl#ip#c#h", "H", 3, NOW)
        (item,) = self.ops.items.values()
        self.assertEqual(item["cnt"], 1)
        self.assertGreater(item["expires_at"], NOW + 3600)

    def test_new_window_resets(self):
        for _ in range(3):
            store.hit(self.ops, "rl#ip#c#h", "H", 3, NOW)
        self.assertFalse(store.hit(self.ops, "rl#ip#c#h", "H", 3, NOW))
        self.assertTrue(store.hit(self.ops, "rl#ip#c#h", "H", 3, NOW + 3600))

    def test_keys_and_kinds_are_independent(self):
        store.hit(self.ops, "rl#ip#a#h", "H", 1, NOW)
        self.assertFalse(store.hit(self.ops, "rl#ip#a#h", "H", 1, NOW))
        self.assertTrue(store.hit(self.ops, "rl#ip#b#h", "H", 1, NOW))
        self.assertTrue(store.hit(self.ops, "rl#ip#a#h", "D", 1, NOW))

    def test_window_ids(self):
        self.assertEqual(store.window_id("H", NOW)[0], "h#2026110608")
        self.assertEqual(store.window_id("D", NOW)[0], "d#20261106")

    def test_other_errors_propagate(self):
        class Boom(FakeTable):
            def update_item(self, **kw):
                from botocore.exceptions import ClientError
                raise ClientError("ProvisionedThroughputExceededException")

        with self.assertRaises(Exception):
            store.hit(Boom("pk", "sk"), "k", "H", 1, NOW)


if __name__ == "__main__":
    unittest.main()
