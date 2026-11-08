"""Public copy rules: disclaimer everywhere, no emoji, no trademark in the title, consent text archived."""

import glob
import hashlib
import html
import json
import os
import re
import unittest

from apibase import ApiTestCase
from fakes import ROOT, SITE_DIR, TEST_PHONE

import signup_api  # noqa: E402

EMOJI = re.compile(
    "[\U0001F000-\U0001FAFF\U0001F1E6-\U0001F1FF\u2300-\u23FF\u2600-\u27BF\u2B00-\u2BFF\uFE0F\u200D]"
)
DISCLAIMER_PHRASE = "unofficial fan project"
CARRIER_CLAUSE = (
    "No mobile information will be shared with third parties or affiliates for marketing or promotional "
    "purposes. All the above categories exclude text messaging originator opt-in data and consent; this "
    "information will not be shared with any third parties."
)
# sha256 of a private number that must never appear in this project.
FORBIDDEN_NUMBER_SHA256 = "42b6406bba39dc29dcc110ec9bd94ab2ac168c6feb1584244bb39b461361e5c0"

HTML_PAGES = sorted(glob.glob(os.path.join(SITE_DIR, "**", "*.html"), recursive=True))
SITE_FILES = sorted(f for f in glob.glob(os.path.join(SITE_DIR, "**", "*"), recursive=True) if os.path.isfile(f))


def text_of(fragment):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", fragment))).strip()


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


class SiteContentTest(unittest.TestCase):
    def test_expected_pages_exist(self):
        rel = {os.path.relpath(p, SITE_DIR) for p in HTML_PAGES}
        for page in ("index.html", "privacy/index.html", "terms/index.html", "sms-terms/index.html",
                     "unsubscribe/index.html", "unsubscribe/done/index.html", "thanks/index.html", "404.html"):
            self.assertIn(page, rel)

    def test_every_page_has_disclaimer_in_footer(self):
        for path in HTML_PAGES:
            footer = re.search(r"<footer.*?</footer>", read(path), re.S)
            self.assertIsNotNone(footer, path)
            self.assertIn(DISCLAIMER_PHRASE, footer.group(0).lower(), path)
            self.assertIn("Fall Out Boy", footer.group(0), path)

    def test_no_emoji_in_site_files(self):
        for path in SITE_FILES:
            if path.endswith((".html", ".css", ".js", ".txt")):
                self.assertIsNone(EMOJI.search(read(path)), path)

    def test_title_tag_brand_rules(self):
        for path in HTML_PAGES:
            title = re.search(r"<title>(.*?)</title>", read(path), re.S).group(1)
            self.assertIn("Mall Rat Alerts", title, path)
            self.assertNotIn("fall out boy", title.lower(), path)
            self.assertIsNone(re.search(r"\bfob\b", title, re.I), path)

    def test_no_rehosted_media(self):
        for path in SITE_FILES:
            self.assertFalse(path.lower().endswith((".jpg", ".jpeg", ".gif", ".webp", ".mp3", ".mp4")), path)
        for path in HTML_PAGES:
            self.assertNotIn("<img", read(path), path)

    def test_homepage_keyword_cta_has_sms_link_and_archived_disclosure(self):
        page = read(os.path.join(SITE_DIR, "index.html"))
        sms_links = re.findall(r'<a [^>]*href="(sms:[^"]*)"[^>]*>', page)
        self.assertGreaterEqual(len(sms_links), 1)
        for href in sms_links:
            self.assertEqual(html.unescape(href), "sms:+18335961548?&body=FOB")
        self.assertIn(">Subscribe Now</a>", page)
        self.assertIn("On a computer? Text FOB to (833) 596-1548 from your phone.", text_of(page))
        disclosures = re.findall(r'<p class="sms-disclosure">(.*?)</p>', page, re.S)
        self.assertEqual(len(disclosures), page.count('class="pill"') - page.count('<button type="submit" class="pill"'))
        archived = read(os.path.join(SITE_DIR, "consent", "sms-keyword-homepage-2026-11-06.txt")).strip()
        for d in disclosures:
            self.assertEqual(text_of(d), archived)
            for phrase in ("By texting FOB", "recurring automated", "Message frequency varies",
                           "Msg & data rates may apply", "Reply STOP to cancel", "HELP for help",
                           "Consent is not a condition of purchase", "18 or older", "Unofficial fan project"):
                self.assertIn(phrase, text_of(d))
            for href in ("/sms-terms/", "/terms/", "/privacy/"):
                self.assertIn(f'href="{href}"', d)

    def _hero(self):
        page = read(os.path.join(SITE_DIR, "index.html"))
        return re.search(r'<section class="hero"[^>]*>(.*?)</section>', page, re.S).group(1)

    def test_homepage_has_phone_pill_and_desktop_form_variants(self):
        hero = self._hero()
        self.assertEqual(hero.count('class="sms-cta cta-phone"'), 1)
        self.assertEqual(hero.count('class="sms-cta cta-desktop"'), 1)
        phone = re.search(r'<div class="sms-cta cta-phone">(.*?)</div>', hero, re.S).group(1)
        desktop = re.search(r'<div class="sms-cta cta-desktop">(.*)', hero, re.S).group(1)
        self.assertIn('href="sms:+18335961548?&amp;body=FOB"', phone)
        self.assertNotIn("<form", phone)
        self.assertIn('<form id="subscribe-form" method="post" action="/api/subscribe">', desktop)
        self.assertNotIn("sms:", desktop)
        self.assertIn("On a computer? Text FOB to (833) 596-1548 from your phone.", text_of(desktop))
        # only one form on the page exposes a pill-styled submit
        self.assertIn('<button type="submit" class="pill">Subscribe Now</button>', desktop)

    def test_discord_cta_placeholder_and_sms_details_order(self):
        page = read(os.path.join(SITE_DIR, "index.html"))
        hero = self._hero()
        self.assertEqual(page.count("<!--DISCORD_CTA-->"), 1)
        self.assertEqual(page.count("<!--SMS_OPEN-->"), 1)
        self.assertIn("<!--DISCORD_CTA-->", hero)
        discord_ph = hero.index("<!--DISCORD_CTA-->")
        sms_details = hero.index('<details class="sms-details" id="sms-signup"')
        self.assertLess(discord_ph, sms_details)
        summary = re.search(r"<summary>(.*?)</summary>", hero[sms_details:], re.S).group(1)
        self.assertEqual(text_of(summary), "Don't have Discord? Join our limited SMS alert spots.")
        sms_block = hero[sms_details:hero.index("</details>", sms_details)]
        self.assertIn('class="sms-disclosure"', sms_block)
        self.assertIn("Spots are limited because each text costs money.", text_of(sms_block))
        for href in ("/sms-terms/", "/terms/", "/privacy/"):
            self.assertIn(f'href="{href}"', sms_block)
        self.assertIn('href="sms:+18335961548?&amp;body=FOB"', sms_block)
        self.assertIn('<form id="subscribe-form"', sms_block)
        # Disclosure and legal links live with the SMS CTA inside the dropdown, not above it.
        before_sms = hero[:sms_details]
        self.assertNotIn('class="sms-disclosure"', before_sms)
        self.assertNotIn('href="sms:', before_sms)

    def test_discord_cta_template_and_empty_invite_fallback(self):
        tpl = read(os.path.join(ROOT, "terraform", "discord-cta.html.tpl"))
        self.assertIn("Join the Discord", tpl)
        self.assertIn("DISCORD_INVITE_URL", tpl)
        self.assertIn('target="_blank"', tpl)
        self.assertIn('rel="noopener"', tpl)
        self.assertIn('data-discord-cta="hero"', tpl)
        self.assertIn("Alerts post there the moment the site changes, plus a channel per release.", text_of(tpl))
        self.assertIsNone(EMOJI.search(tpl))
        invite = "https://discord.gg/EXAMPLE"
        filled = tpl.replace("DISCORD_INVITE_URL", invite)
        self.assertIn(f'href="{invite}"', filled)
        page = read(os.path.join(SITE_DIR, "index.html"))
        # Invite set: Discord CTA appears before SMS details; SMS stays closed.
        with_invite = page.replace("<!--DISCORD_CTA-->", filled).replace("<!--SMS_OPEN-->", "")
        hero_with = re.search(r'<section class="hero"[^>]*>(.*?)</section>', with_invite, re.S).group(1)
        self.assertLess(hero_with.index("Join the Discord"), hero_with.index('class="sms-details"'))
        self.assertIn(f'href="{invite}"', hero_with)
        self.assertRegex(hero_with, r'<details class="sms-details" id="sms-signup">')
        self.assertNotRegex(hero_with, r'<details class="sms-details" id="sms-signup"\s+open')
        # Invite empty: Discord CTA gone; SMS open as today.
        without = page.replace("<!--DISCORD_CTA-->", "").replace("<!--SMS_OPEN-->", " open")
        hero_without = re.search(r'<section class="hero"[^>]*>(.*?)</section>', without, re.S).group(1)
        self.assertNotIn("Join the Discord", hero_without)
        self.assertNotIn("discord-cta", hero_without)
        self.assertRegex(hero_without, r'<details class="sms-details" id="sms-signup"\s+open')
        self.assertIn('href="sms:+18335961548?&amp;body=FOB"', hero_without)
        self.assertIn('<form id="subscribe-form"', hero_without)

    def test_device_split_is_css_with_phone_default_off(self):
        css = read(os.path.join(SITE_DIR, "assets", "style.css"))
        self.assertRegex(css, r"\.cta-phone\s*\{\s*display:\s*none;\s*\}")
        m = re.search(r"@media \(hover: none\) and \(pointer: coarse\)\s*\{(.*?)\n\}", css, re.S)
        self.assertIsNotNone(m)
        self.assertRegex(m.group(1), r"\.cta-phone\s*\{\s*display:\s*block;")
        self.assertRegex(m.group(1), r"\.cta-desktop\s*\{\s*display:\s*none;")

    def test_desktop_form_consent_and_age_boxes_are_unchecked_and_full_disclosure(self):
        hero = self._hero()
        boxes = re.findall(r'<input type="checkbox"[^>]*>', hero)
        self.assertEqual(len(boxes), 2)
        for box in boxes:
            self.assertNotIn("checked", box)
            self.assertIn("required", box)
        self.assertIn('type="tel"', hero)
        self.assertIn('autocomplete="tel"', hero)
        self.assertIn('name="campaign_id" value="fob-twilight-town-mall-2026"', hero)
        self.assertIn(f'name="consent_version" value="{os.environ["CONSENT_VERSION"]}"', hero)
        label = text_of(re.search(r'<label for="consent" id="consent-text">(.*?)</label>', hero, re.S).group(1))
        archived = read(os.path.join(SITE_DIR, "consent", f'{os.environ["CONSENT_VERSION"]}.txt')).strip()
        self.assertEqual(label, archived)
        for phrase in ("text this number once to confirm", "reply YES", "Msg frequency varies",
                       "Msg & data rates may apply", "STOP", "HELP", "not a condition of any purchase"):
            self.assertIn(phrase, label)
        for href in ("/sms-terms/", "/terms/", "/privacy/"):
            self.assertIn(f'href="{href}"', hero)
        self.assertIn("<!--TURNSTILE_WIDGET-->", hero)

    def test_homepage_posthog_events_are_phoneless(self):
        js = read(os.path.join(SITE_DIR, "assets", "app.js"))
        for ev in ('kind + "_submit"', 'kind + "_success"', 'kind + "_error"'):
            self.assertIn(ev, js)
        self.assertIn('device: "desktop"', js)
        self.assertNotRegex(js, r"track\([^)]*phone")

    def test_hero_has_pill_fallback_and_disclosure(self):
        page = read(os.path.join(SITE_DIR, "index.html"))
        hero = re.search(r'<section class="hero"[^>]*>(.*?)</section>', page, re.S).group(1)
        self.assertIn('href="sms:+18335961548?&amp;body=FOB"', hero)
        self.assertIn('class="sms-disclosure"', hero)
        self.assertIn("On a computer? Text FOB to (833) 596-1548 from your phone.", text_of(hero))
        for href in ("/sms-terms/", "/terms/", "/privacy/"):
            self.assertIn(f'href="{href}"', hero)

    def test_homepage_sections_are_collapsible_with_recent_alerts_open(self):
        page = read(os.path.join(SITE_DIR, "index.html"))
        shelves = re.findall(r'<details class="shelf"([^>]*)>\s*<summary>(.*?)</summary>', page, re.S)
        labels = {text_of(label): attrs for attrs, label in shelves}
        for name in ("Recent alerts", "How it works", "FAQ"):
            self.assertIn(name, labels)
        self.assertNotIn("Tip jar", labels)
        self.assertRegex(labels["Recent alerts"], r"\bopen\b")
        for name, attrs in labels.items():
            if name != "Recent alerts":
                self.assertNotRegex(attrs, r"\bopen\b", name)
        recent = page[page.index('<details class="shelf" id="recent"'):]
        recent = recent[:recent.index("</details>")]
        self.assertIn('id="alert-list"', recent)

    def test_tip_banner_is_near_top_and_placeholder_driven(self):
        page = read(os.path.join(SITE_DIR, "index.html"))
        header_end = page.index("</header>")
        main_start = page.index("<main")
        tip_ph = page.index("<!--TIP_JAR-->")
        self.assertGreater(tip_ph, header_end)
        self.assertLess(tip_ph, main_start)
        self.assertEqual(page.count("<!--TIP_JAR-->"), 1)
        tip = read(os.path.join(ROOT, "terraform", "tip-jar.html.tpl"))
        self.assertIn('id="tip-jar"', tip)
        self.assertIn('class="support-banner"', tip)
        self.assertNotIn("<details", tip)
        self.assertIn("STRIPE_PAYMENT_LINK", tip)
        self.assertIn('target="_blank"', tip)
        self.assertIn('rel="noopener"', tip)
        self.assertIn("Buy me a coffee", tip)
        self.assertIn('data-tip-cta="top_banner"', tip)
        self.assertIn("Mall Rat Alerts is free", tip)
        self.assertIn("pay for every text out of my own pocket", tip)
        self.assertIsNone(EMOJI.search(tip))
        filled = tip.replace("STRIPE_PAYMENT_LINK", "https://example.com/tip")
        self.assertIn('href="https://example.com/tip"', filled)
        # No duplicate tip button in the collapsible shelves.
        shelves = page[page.index('<div class="shelves">'):page.index("</main>")]
        self.assertNotIn("TIP_JAR", shelves)
        self.assertNotIn("tip-jar", shelves)

    def test_homepage_and_thanks_have_no_waitlist_wording(self):
        banned = re.compile(
            r"waitlist|haven't started|have not started|texts have not started|"
            r"start at launch|alerts start at launch|on the list|"
            r"alerts start when we launch",
            re.I,
        )
        for rel in ("index.html", "thanks/index.html"):
            body = read(os.path.join(SITE_DIR, rel))
            self.assertIsNone(banned.search(body), rel)
        js = read(os.path.join(SITE_DIR, "assets", "app.js"))
        self.assertIsNone(banned.search(js))
        self.assertIn("Alerts are live and go out by text when the site changes", js)
        home = read(os.path.join(SITE_DIR, "index.html"))
        self.assertIn("Alerts are live and go out by text when the site changes", home)

    def test_web_consent_archives_published(self):
        # Earlier web sign-ups reference v1 and its hash, so it stays unchanged; v2 is the current form text.
        for v in ("v1", "v2"):
            self.assertTrue(os.path.exists(os.path.join(SITE_DIR, "consent", f"sms-consent-2026-11-06.{v}.txt")))
        self.assertEqual(os.environ["CONSENT_VERSION"], "sms-consent-2026-11-06.v2")

    def test_privacy_has_carrier_clause_verbatim(self):
        self.assertIn(CARRIER_CLAUSE, text_of(read(os.path.join(SITE_DIR, "privacy", "index.html"))))

    def test_sms_terms_required_items(self):
        t = text_of(read(os.path.join(SITE_DIR, "sms-terms", "index.html")))
        for phrase in ("Message frequency varies", "Msg & data rates may apply", "STOP", "HELP",
                       "Carriers are not liable for delayed or undelivered messages",
                       "hello@mallratalerts.com", "18 or older", "START"):
            self.assertIn(phrase, t)

    def test_unsubscribe_page_is_per_campaign(self):
        t = text_of(read(os.path.join(SITE_DIR, "unsubscribe", "index.html")))
        self.assertIn("only opts you out of this campaign", t)

    def test_legal_placeholders_removed_not_invented(self):
        for page in ("privacy/index.html", "terms/index.html", "sms-terms/index.html"):
            body = read(os.path.join(SITE_DIR, page))
            for placeholder in ("[OPERATOR", "[GOVERNING"):
                self.assertNotIn(placeholder, body, page)

    def test_text_to_join_is_documented_on_homepage_and_terms(self):
        home = text_of(read(os.path.join(SITE_DIR, "index.html")))
        self.assertIn("Text FOB", home)
        self.assertIn("(833) 596-1548", home)
        self.assertIn("sms:+18335961548", read(os.path.join(SITE_DIR, "index.html")))
        terms = text_of(read(os.path.join(SITE_DIR, "sms-terms", "index.html")))
        self.assertIn("By text:", terms)
        self.assertIn("FOB", terms)
        self.assertIn("We then send one text asking you to reply YES", terms)
        self.assertIn("at most one confirmation text per number per 24 hours", terms)
        self.assertIn("text START or FOB from your phone to rejoin", terms)
        archived = read(os.path.join(SITE_DIR, "consent", "sms-keyword-v1.txt"))
        for phrase in ("Msg & data rates may apply", "STOP", "HELP", "18 or older"):
            self.assertIn(phrase, archived)

    def test_no_ai_disclosure_anywhere_on_the_site(self):
        pattern = re.compile(r"written by ai|ai-generated|generated by ai|summari[sz]ed by (a )?(model|ai)|\bAI\b", re.I)
        for path in SITE_FILES:
            if path.endswith((".html", ".css", ".js", ".txt")):
                self.assertIsNone(pattern.search(read(path)), path)
        self.assertIsNone(pattern.search(read(os.path.join(ROOT, "lambda", "common", "default_public_prompt.txt"))))
        for code, msg in signup_api.MESSAGES.items():
            self.assertIsNone(pattern.search(msg), code)

    def test_design_system_assets_are_self_hosted(self):
        css = read(os.path.join(SITE_DIR, "assets", "style.css"))
        self.assertNotRegex(css, r"https?://")
        for name in ("anton-400", "atkinson-400", "atkinson-700", "caveat-brush-400", "plex-mono-600"):
            self.assertTrue(os.path.exists(os.path.join(SITE_DIR, "assets", "fonts", name + ".woff2")))
            self.assertIn(f"/assets/fonts/{name}.woff2", css)
        for lic in ("anton", "atkinsonhyperlegible", "caveatbrush", "ibmplexmono"):
            self.assertIn("SIL Open Font License", read(os.path.join(SITE_DIR, "assets", "fonts", f"LICENSE-{lic}.txt")))
        for path in HTML_PAGES:
            self.assertIn("/assets/style.css", read(path), path)
            self.assertIn('class="site-header"', read(path), path)

    def test_homepage_has_recent_alerts_feed_with_graceful_states(self):
        home = read(os.path.join(SITE_DIR, "index.html"))
        self.assertIn('id="alert-list"', home)
        self.assertIn("<noscript>", home)
        js = read(os.path.join(SITE_DIR, "assets", "app.js"))
        self.assertIn("America/Los_Angeles", js)
        self.assertIn('"/api/alerts?campaign_id=" + encodeURIComponent(cid) + "&limit=50"', js)
        self.assertIn('cache: "no-store"', js)
        self.assertIn('data-campaign="fob-twilight-town-mall-2026"', home)
        self.assertNotIn("innerHTML = a.", js)

    def test_unsubscribe_form_works_without_js(self):
        page = read(os.path.join(SITE_DIR, "unsubscribe", "index.html"))
        self.assertRegex(page, r'<form id="unsubscribe-form" method="post" action="/api/unsubscribe">')
        self.assertIn('name="campaign_id" value="fob-twilight-town-mall-2026"', page)
        for box in re.findall(r'<input type="checkbox"[^>]*>', page):
            self.assertNotIn("checked", box)

    def test_sms_click_is_tracked_without_blocking_the_link(self):
        js = read(os.path.join(SITE_DIR, "assets", "app.js"))
        self.assertIn('"subscribe_sms_click"', js)
        self.assertIn('var campaignId = "fob-twilight-town-mall-2026"', js)
        listener = js[js.index("[data-sms-cta]"):js.index("subscribe_sms_click")]
        self.assertNotIn("preventDefault", listener)

    def test_tip_click_is_tracked_without_blocking_the_link(self):
        js = read(os.path.join(SITE_DIR, "assets", "app.js"))
        self.assertIn('"tip_click"', js)
        self.assertIn('placement: link.getAttribute("data-tip-cta")', js)
        self.assertIn('"top_banner"', js)
        tip_block = js[js.index("[data-tip-cta]"):js.index("[data-discord-cta]")]
        self.assertNotIn("preventDefault", tip_block)
        self.assertIn("tip_click", tip_block)

    def test_discord_join_and_sms_details_events_do_not_block(self):
        js = read(os.path.join(SITE_DIR, "assets", "app.js"))
        self.assertIn('"discord_join_click"', js)
        self.assertIn('"sms_details_open"', js)
        self.assertIn('placement: link.getAttribute("data-discord-cta")', js)
        discord_block = js[js.index("[data-discord-cta]"):js.index("details.sms-details")]
        self.assertNotIn("preventDefault", discord_block)
        self.assertIn("discord_join_click", discord_block)
        sms_block = js[js.index("details.sms-details"):js.index("if (window.fetch)")]
        self.assertNotIn("preventDefault", sms_block)
        self.assertIn("sms_details_open", sms_block)
        self.assertIn('placement: "hero"', sms_block)

    def test_forbidden_private_number_absent_everywhere(self):
        skip = (".terraform", "build", ".git", "__pycache__")
        for dirpath, dirnames, filenames in os.walk(ROOT):
            dirnames[:] = [d for d in dirnames if d not in skip]
            for name in filenames:
                path = os.path.join(dirpath, name)
                try:
                    content = read(path)
                except (UnicodeDecodeError, OSError):
                    continue
                digits = re.sub(r"[\s().\-+]", "", content)
                for run in re.findall(r"\d{10,11}", digits):
                    tail = run[-10:]
                    self.assertNotEqual(hashlib.sha256(tail.encode()).hexdigest(), FORBIDDEN_NUMBER_SHA256, path)


class ApiResponseContentTest(ApiTestCase):
    def test_no_emoji_in_any_api_message(self):
        for code, msg in signup_api.MESSAGES.items():
            self.assertIsNone(EMOJI.search(msg), code)
            for status in (202, 400):
                for is_form in (False, True):
                    res = signup_api.respond(status, code, is_form)
                    self.assertIsNone(EMOJI.search(res["body"]), code)
                    if not is_form:
                        json.loads(res["body"])

    def test_html_responses_have_disclaimer_and_no_trademark_title(self):
        res = signup_api.respond(400, "invalid_phone", True)
        self.assertIn(DISCLAIMER_PHRASE, res["body"].lower())
        title = re.search(r"<title>(.*?)</title>", res["body"]).group(1)
        self.assertNotIn("Fall Out Boy", title)

    def test_live_handler_responses_have_no_emoji(self):
        for payload in (self.signup(), self.signup(phone="bad"), {"phone": TEST_PHONE}):
            for path in ("/api/subscribe", "/api/unsubscribe"):
                res = self.call(path, payload)
                self.assertIsNone(EMOJI.search(res["body"] or ""), path)


if __name__ == "__main__":
    unittest.main()
