"""Deterministic pre-filter for the public alert composer.

Runs BEFORE the model call. If every change in an event is clearly unimportant to a fan, the composer
stores a suppressed audit row (status=suppressed, suppressed_reason="prefilter:<rule>") and never calls
Bedrock, fans out, or posts to Discord. Anything not obviously junk goes on to the model, which applies
the conservative significance guidance in default_public_prompt.txt.

Input is the "changes" list from the site monitor (fob-site-monitor diff_page): per page a dict with "url"
plus any of status/location/title/visit_clickable ({"before","after"}), links_added/links_removed,
media_added/media_removed, visible_text_diff, html_diff, new_linked_pages, new_page_excerpt.

Deliberately small and conservative: a reason is returned only when ALL changes in the event match a rule.
One page that might matter keeps the whole event for the model. Malformed input is never filtered.
"""

import re
from urllib.parse import urlparse

QUIET_STATUSES = {403, 404, 410}
REDIRECTS = {301, 302, 307, 308}
SYSTEM_PATHS = {"/sitemap.xml", "/robots.txt"}
# Pages a fan actually looks at: a status change here is never filtered.
PRIMARY_PATHS = {"/", "/tour", "/events", "/boxoffice", "/offers", "/music", "/store", "/shop",
                 "/lost-and-found", "/foodcourt", "/visit", "/sign-up"}
BULK_LINKS = 5
NODE_PATH = re.compile(r"^/node/\d+$")
# New link targets containing these are handed to the model even when the change is "just a link".
FAN_WORDS = re.compile(
    r"\b(tour|tickets?|presale|album|single|song|music|video|vinyl|merch|store|shop|stream|"
    r"pre-?order|aces|puzzle|password|unlock)", re.I)
# Quoted JS/HTML tokens that look like places or dates (tour region lists often live only in markup).
_QUOTED = re.compile(r'"([^"\\]{1,60})"')
_MONTH = re.compile(
    r"^(January|February|March|April|May|June|July|August|September|October|November|December|"
    r"Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)\.?$", re.I)
_DATE_LIKE = re.compile(
    r"^(\d{1,2}[/-]\d{1,2}([/-]\d{2,4})?|\d{4}-\d{2}-\d{2}|\d{1,2}\s+"
    r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?(\s+\d{2,4})?)$", re.I)
_REGION_CODE = re.compile(r"^[A-Z]{2}$")
_PLACE_NAME = re.compile(r"^[A-Z][A-Za-z]+(?:[ '-][A-Z][A-Za-z]+)*$")


def _path(url):
    try:
        p = urlparse(url or "").path or "/"
    except ValueError:
        return ""
    return p.rstrip("/") or "/"


def _only(change, allowed):
    """True when the change has no keys beyond `allowed` (+ url and the raw markup diff)."""
    return set(change) <= set(allowed) | {"url", "html_diff"}


def _after(change, key):
    v = change.get(key)
    return v.get("after") if isinstance(v, dict) else None


def _before(change, key):
    v = change.get(key)
    return v.get("before") if isinstance(v, dict) else None


def _stem(url):
    """File name without extension or query, so image.webp -> image.jpg or a CDN host move is a 'swap'."""
    name = urlparse(url).path.rsplit("/", 1)[-1]
    return name.rsplit(".", 1)[0].lower()


def _asset_swap(change):
    """media_added/media_removed that are the same files re-hosted or re-encoded (no new image)."""
    added, removed = change.get("media_added") or [], change.get("media_removed") or []
    return bool(added or removed) and {_stem(u) for u in added} == {_stem(u) for u in removed}


def _title_echo(change):
    """visible_text_diff whose changed lines are only the old/new page title (the title is the first
    visible line, so a title tweak always shows up there too)."""
    diff = change.get("visible_text_diff")
    if not diff:
        return True
    titles = {_before(change, "title"), _after(change, "title")}
    lines = [l[1:].strip() for l in diff.splitlines()
             if l[:1] in "+-" and not l.startswith(("+++", "---"))]
    return all(l in titles for l in lines)


def _diff_sides(diff):
    """Split a unified diff into (added_text, removed_text), ignoring file headers."""
    added, removed = [], []
    for line in (diff or "").splitlines():
        if line.startswith(("+++", "---")):
            continue
        if line.startswith("+"):
            added.append(line[1:])
        elif line.startswith("-"):
            removed.append(line[1:])
    return "\n".join(added), "\n".join(removed)


def _place_or_date_token(token):
    """True for country/city-like names, 2-letter region codes, month names, or date-like strings."""
    t = (token or "").strip()
    if not t:
        return False
    if _MONTH.match(t) or _DATE_LIKE.match(t) or _REGION_CODE.match(t):
        return True
    return bool(_PLACE_NAME.match(t))


def _new_place_or_date_content(change):
    """True when html_diff / visible_text_diff adds a place or date token absent from the removed side.

    Tour region maps often live only in page JS, so a markup-only diff that newly quotes \"Czechia\"
    or \"Hungary\" is still worth a model look. Reorders of the same tokens do not match.
    """
    added_parts, removed_parts = [], []
    for key in ("html_diff", "visible_text_diff"):
        a, r = _diff_sides(change.get(key))
        if a:
            added_parts.append(a)
        if r:
            removed_parts.append(r)
    added, removed = "\n".join(added_parts), "\n".join(removed_parts)
    if not added:
        return False
    removed_quoted = set(_QUOTED.findall(removed))
    for tok in _QUOTED.findall(added):
        if tok not in removed_quoted and _place_or_date_token(tok):
            return True
    # Unquoted month/date fragments in added lines (e.g. +Nov 20 - Chicago) that are new.
    removed_l = removed.lower()
    for line in added.splitlines():
        for word in re.findall(r"[A-Za-z]{3,}|[\d][^\s,]{0,20}", line):
            if _place_or_date_token(word) and word.lower() not in removed_l:
                return True
    return False


def _rule_for(change):
    """Return the rule name when this single change is junk, else None."""
    path = _path(change.get("url"))
    keys = set(change)

    if path in SYSTEM_PATHS:
        return "system_file"

    # Hidden, admin or numbered pages flipping 403/404/410, or a /node/N alias starting to redirect.
    if "status" in keys and path not in PRIMARY_PATHS and _only(change, {"status", "location", "title"}):
        before, after = _before(change, "status"), _after(change, "status")
        if before in QUIET_STATUSES and after in QUIET_STATUSES:
            return "status_flip"
        if NODE_PATH.match(path) and after in REDIRECTS | QUIET_STATUSES:
            return "status_flip"

    # Title/meta tweak, optionally with the same images re-hosted or re-encoded.
    if keys & {"title", "media_added", "media_removed"} \
            and _only(change, {"title", "visible_text_diff", "media_added", "media_removed"}) \
            and _title_echo(change) \
            and (not keys & {"media_added", "media_removed"} or _asset_swap(change)):
        return "title_or_asset_swap"

    # Nothing a visitor can see changed (scripts, styles, hashes, whitespace) — unless the markup
    # newly introduces place/date tokens (tour region JS lists, embedded date strings).
    if keys <= {"url", "html_diff"}:
        if _new_place_or_date_content(change):
            return None
        return "markup_only"

    # Only links changed: no new page was followed, no new visible text, no new media.
    if keys & {"links_added", "links_removed"} and _only(change, {"links_added", "links_removed", "title"}):
        added, removed = change.get("links_added") or [], change.get("links_removed") or []
        # A bulk add/remove looks like a theme or navigation overhaul, so the model gets to judge it.
        if len(added) < BULK_LINKS and len(removed) < BULK_LINKS and not any(FAN_WORDS.search(u) for u in added):
            return "link_only"

    return None


def evaluate(changes):
    """Return a short reason such as "status_flip" when every change is unimportant, else None.

    An empty or malformed list is NOT filtered, so a monitor format change can never silently swallow alerts.
    """
    if not isinstance(changes, list) or not changes:
        return None
    rules = []
    for c in changes:
        if not isinstance(c, dict):
            return None
        rule = _rule_for(c)
        if rule is None:
            return None
        rules.append(rule)
    return rules[0] if len(set(rules)) == 1 else "mixed_minor"
