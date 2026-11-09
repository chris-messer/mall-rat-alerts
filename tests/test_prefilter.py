"""Deterministic pre-filter: obviously unimportant site changes never reach the model.

Fixtures mimic real monitor diffs (shortened) from the Twilight Town Mall rollout.
"""

import unittest

from fakes import install_stubs

install_stubs()

from common import prefilter  # noqa: E402

H = "https://www.falloutboy.com"
MALL = "https://twilighttownmall.falloutboy.com/"


def flip(path, before=403, after=404):
    return {"url": H + path, "status": {"before": before, "after": after},
            "title": {"before": "Twilight Town Mall | Not authorized", "after": "Fall Out Boy | Page not found"}}


class SuppressedTest(unittest.TestCase):
    def check(self, changes, reason):
        self.assertEqual(prefilter.evaluate(changes), reason)

    def test_hidden_pages_403_to_404(self):
        self.check([flip("/phase2"), flip("/node/706"), flip("/node/716")], "status_flip")

    def test_410_and_404_flips_on_non_primary_pages(self):
        self.check([flip("/secret", 404, 410)], "status_flip")

    def test_node_alias_starting_to_redirect(self):
        self.check([{"url": H + "/node/746", "status": {"before": 404, "after": 301},
                     "location": {"before": None, "after": H + "/aces-hifi-records"},
                     "title": {"before": "Page not found", "after": "Redirecting to ..."}}], "status_flip")

    def test_sitemap_and_robots(self):
        sitemap = {"url": H + "/sitemap.xml", "html_diff": "-<loc>a</loc>\n+<loc>https://other.example/</loc>",
                   "visible_text_diff": "-https://www.falloutboy.com/tour\n+https://other.example/tour"}
        self.check([sitemap], "system_file")
        self.check([{"url": H + "/robots.txt", "visible_text_diff": "+Disallow: /"}], "system_file")

    def test_title_only_and_title_echo_in_visible_text(self):
        self.check([{"url": H + "/boxoffice", "title": {"before": "BoxOffice | Twilight Town Mall",
                                                          "after": "Twilight Town Mall | BoxOffice"},
                     "visible_text_diff": "@@ -1,2 +1,2 @@\n-BoxOffice | Twilight Town Mall\n"
                                          "+Twilight Town Mall | BoxOffice\n Skip to main content",
                     "html_diff": "-<meta name=\"description\" ..."}], "title_or_asset_swap")

    def test_same_image_rehosted_or_reencoded(self):
        self.check([{"url": H + "/aces-hifi-records",
                     "media_added": ["https://cdn.example/atl/aceshifi.jpg"],
                     "media_removed": ["https://cdn.example/atl/aceshifi.webp"]}], "title_or_asset_swap")
        self.check([{"url": H + "/lost-and-found", "title": {"before": "A | B", "after": "B | A"},
                     "media_added": [H + "/sites/g/files/g2000024396/files/2026-11/lostandfound_x.svg"],
                     "media_removed": [H + "/sites/g/files/g2000024416/files/2026-11/lostandfound_x.svg"]}],
                   "title_or_asset_swap")

    def test_markup_only(self):
        self.check([{"url": H + "/", "html_diff": "-js-view-dom-id-aaa\n+js-view-dom-id-bbb"}], "markup_only")

    def test_link_only_one_for_one_swap_across_pages(self):
        nav = {"links_added": [MALL], "links_removed": ["javascript:void(0)"],
               "html_diff": "-<a href=\"javascript:void(0)\">Twilight Town Mall</a>\n+<a href=\"" + MALL + "\">"}
        self.check([{"url": H + "/events", **nav}, {"url": H + "/offers", **nav}], "link_only")

    def test_mixed_minor_labelled_generic(self):
        self.check([flip("/phase2"), {"url": H + "/", "html_diff": "-x\n+y"}], "mixed_minor")


class KeptForModelTest(unittest.TestCase):
    def keep(self, changes):
        self.assertIsNone(prefilter.evaluate(changes))

    def test_homepage_theme_switch_with_tour_and_single(self):
        self.keep([{"url": H + "/", "title": {"before": "Twilight Town Mall | Homepage", "after": "Fall Out Boy"},
                    "visit_clickable": {"before": False, "after": None},
                    "links_added": ["https://FallOutBoy.lnk.to/ACES", H + "/tour"],
                    "links_removed": [H + "/events", H + "/offers"],
                    "media_added": [H + "/sites/g/files/x/files/fobalbumart.jpg"],
                    "visible_text_diff": "+NEW SINGLE: ACES - OUT NOW\n+PRE-SALE:"}])

    def test_new_text_or_new_page_content_is_kept(self):
        self.keep([{"url": H + "/tour", "visible_text_diff": "+Nov 20 - Chicago"}])
        self.keep([{"url": H + "/", "links_added": [H + "/aces-hifi-records"],
                    "new_linked_pages": [{"url": H + "/aces-hifi-records", "status": 200, "excerpt": "store 04"}]}])

    def test_newly_reachable_hidden_page_is_kept(self):
        self.keep([{"url": H + "/node/812", "status": {"before": 404, "after": 200},
                    "title": {"before": "Page not found", "after": "Basement"},
                    "new_page_excerpt": "You found it. Enter the code."}])
        # A 200 with no excerpt is still not a quiet flip.
        self.keep([{"url": H + "/node/812", "status": {"before": 404, "after": 200}}])

    def test_status_change_on_primary_page_is_kept(self):
        self.keep([flip("/tour", 200, 404)])
        self.keep([flip("/events")])

    def test_new_image_or_new_media_is_kept(self):
        self.keep([{"url": H + "/music", "media_added": [H + "/files/new_cover.jpg"]}])
        self.keep([{"url": H + "/music", "media_added": [H + "/a/one.jpg"], "media_removed": [H + "/a/two.jpg"]}])

    def test_fan_facing_link_targets_are_kept(self):
        for href in ("https://falloutboy.lnk.to/store", "https://falloutboy.lnk.to/music",
                     "https://tickets.example/tour", "https://FallOutBoy.lnk.to/ACES"):
            self.keep([{"url": H + "/", "links_added": [href]}])

    def test_bulk_link_overhaul_is_kept(self):
        self.keep([{"url": H + "/", "links_removed": [f"{H}/p{i}" for i in range(6)]}])

    def test_one_important_change_keeps_whole_event(self):
        self.keep([flip("/phase2"), {"url": H + "/tour", "visible_text_diff": "+Tickets on sale"}])

    def test_title_change_with_real_text_change_is_kept(self):
        self.keep([{"url": H + "/", "title": {"before": "A", "after": "B"}, "visible_text_diff": "-old\n+Album out now"}])

    def test_empty_or_malformed_input_is_never_filtered(self):
        for bad in (None, [], {}, "x", [None], [{"url": H + "/", "html_diff": "x"}, "oops"]):
            self.keep(bad)


if __name__ == "__main__":
    unittest.main()
