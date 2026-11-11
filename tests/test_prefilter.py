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

    def test_markup_only_region_reorder_with_no_new_tokens(self):
        # Same quoted country/region tokens, just reordered in the JS array — still markup_only.
        diff = ('-                "Czech Republic", "Czechia", "CZ",\n'
                '-                "Hungary", "HU"\n'
                '+                "Hungary", "HU",\n'
                '+                "Czech Republic", "Czechia", "CZ"')
        self.check([{"url": H + "/tour", "html_diff": diff}], "markup_only")

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

    def test_tour_page_new_region_names_reach_the_model(self):
        # Fixture from 2026-11-09 ~18:51Z: region labels appearing on /tour (and mirrored pages).
        # Must not be prefilter link_only / title_or_asset_swap; the model decides notify.
        vtd = ("@@ -8,5 +8,2 @@\n Twilight Town Mall\n-All\n-US + Canada\n-EU+UK\n PRE-SALE:\n"
               "@@ -19,2 +16,5 @@\n NORTH AMERICA GENERAL ON SALE BEGINS THUR NOV 12 @ 10AM LOCAL TIME\n"
               "+All\n+US + Canada\n+EU+UK\n view more dates")
        added = ("@@ -8,4 +8,7 @@\n Twilight Town Mall\n All\n US + Canada\n EU+UK\n"
                 "+Australia + NZ\n+Latin America\n+Asia\n PRE-SALE:\n view more dates")
        self.keep([{"url": H + "/", "visible_text_diff": vtd, "html_diff": "+EU+UK"},
                   {"url": H + "/tour", "visible_text_diff": vtd, "html_diff": "+EU+UK"},
                   {"url": H + "/?frontpage=true", "visible_text_diff": vtd, "html_diff": "+EU+UK"}])
        self.keep([{"url": H + "/tour", "visible_text_diff": added, "html_diff": "+Australia + NZ"}])

    def test_js_tour_region_country_additions_reach_the_model(self):
        # Event e48cce11 (2026-11-09 19:31Z): europe JS array gained Czechia + Hungary with no
        # visible_text_diff. Must not be prefilter:markup_only — the model decides notify.
        diff = ('-                "Czech Republic", "CZ",\n'
                '+                "Czech Republic", "Czechia", "CZ",\n'
                '+                "Hungary", "HU"')
        pages = [
            {"url": H + "/", "html_diff": diff},
            {"url": H + "/tour", "html_diff": diff},
            {"url": H + "/?frontpage=true", "html_diff": diff},
        ]
        self.keep(pages)
        self.keep([{"url": H + "/tour", "html_diff": diff}])

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


M = "https://twilighttownmall.falloutboy.com"


class MallHostTest(unittest.TestCase):
    """twilighttownmall.falloutboy.com is first-class: same junk rules, real content reaches the model."""

    def test_junk_on_mall_host_is_still_suppressed(self):
        flip_mall = {"url": M + "/phase2", "status": {"before": 403, "after": 404}}
        self.assertEqual(prefilter.evaluate([flip_mall]), "status_flip")
        self.assertEqual(prefilter.evaluate([{"url": M + "/", "html_diff": "-js-view-dom-id-aaa\n+js-view-dom-id-bbb"}]),
                         "markup_only")
        self.assertEqual(prefilter.evaluate([{"url": M + "/sitemap.xml", "sitemap_urls_added": [M + "/new"]}]),
                         "system_file")
        self.assertEqual(prefilter.evaluate([{"url": M + "/foodcourt", "title": {"before": "A", "after": "B"}}]),
                         "title_or_asset_swap")

    def test_cookie_banner_markup_on_mall_host_is_suppressed(self):
        self.assertEqual(prefilter.evaluate([{"url": M + "/", "html_diff":
                         "-<div id=\"onetrust-banner-sdk\">Cookies</div>\n+<div id=\"onetrust-banner-sdk\" class=\"x\">Cookies</div>"}]),
                         "markup_only")

    def test_real_mall_content_is_kept_for_the_model(self):
        for change in (
            {"url": M + "/lost-and-found", "visible_text_diff": "+A new note was found: meet at midnight"},
            {"url": M + "/", "links_added": [M + "/newstore"],
             "new_linked_pages": [{"url": M + "/newstore", "status": 200, "excerpt": "STORE 07"}]},
            {"url": M + "/", "status": {"before": 503, "after": 200}},
            {"url": M + "/phase2", "status": {"before": 403, "after": 200},
             "new_page_excerpt": "Phase 2 begins"},
            {"url": M + "/boxoffice", "media_added": [M + "/sites/g/files/x/files/new-poster.png"]},
        ):
            self.assertIsNone(prefilter.evaluate([change]), change)

    def test_main_site_redirect_to_mall_still_reaches_model_on_primary_pages(self):
        # Already-baselined moves are quiet; a primary page flipping to a redirect is judged by the model.
        move = {"url": H + "/boxoffice", "status": {"before": 200, "after": 301},
                "location": {"before": None, "after": M + "/boxoffice"}}
        self.assertIsNone(prefilter.evaluate([move]))

    def test_tickets_on_main_site_still_kept(self):
        self.assertIsNone(prefilter.evaluate([{"url": H + "/tour", "visible_text_diff": "+Nov 20 - Chicago ON SALE"}]))


if __name__ == "__main__":
    unittest.main()
