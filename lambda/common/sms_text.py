"""SMS body sanitizer and segment counter (stdlib only).

Two modes:
  public   GSM-7 only. Smart punctuation is mapped to ASCII, accents are folded with NFKD,
           and anything still outside GSM-7 (emoji included) is dropped.
  private  Same punctuation and space mapping, but emoji and other characters are kept
           (the message may go out as UCS-2).

Segment math: GSM-7 is 160 units in one segment, 153 per segment when split, and the
extension characters ^{}\\[~]|EUR count as 2 units. UCS-2 is 70 UTF-16 code units in one
segment, 67 per segment when split (emoji outside the BMP count as 2).

This file is kept identical in fob-site-monitor/lambda/ and mall-rat-alerts/lambda/common/.
"""

import math
import re
import unicodedata

GSM7_BASIC = set(
    "@\u00a3$\u00a5\u00e8\u00e9\u00f9\u00ec\u00f2\u00c7\n\u00d8\u00f8\r\u00c5\u00e5\u0394_\u03a6\u0393\u039b\u03a9"
    "\u03a0\u03a8\u03a3\u0398\u039e\u00c6\u00e6\u00df\u00c9 !\"#\u00a4%&'()*+,-./0123456789:;<=>?\u00a1"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ\u00c4\u00d6\u00d1\u00dc\u00a7\u00bfabcdefghijklmnopqrstuvwxyz"
    "\u00e4\u00f6\u00f1\u00fc\u00e0"
)
GSM7_EXT = set("^{}\\[~]|\u20ac\f")

_DASH = "-"
REPLACEMENTS = {
    # dashes and minus
    "\u2010": _DASH, "\u2011": _DASH, "\u2012": _DASH, "\u2013": _DASH, "\u2014": _DASH,
    "\u2015": _DASH, "\u2212": _DASH, "\u2e3a": "--", "\u2e3b": "---", "\ufe58": _DASH,
    "\ufe63": _DASH, "\uff0d": _DASH,
    # single quotes and primes
    "\u2018": "'", "\u2019": "'", "\u201a": "'", "\u201b": "'", "\u2032": "'", "\u2035": "'",
    "\u00b4": "'", "`": "'", "\u02bc": "'", "\uff07": "'",
    # double quotes
    "\u201c": '"', "\u201d": '"', "\u201e": '"', "\u201f": '"', "\u2033": '"', "\u2036": '"',
    "\u00ab": '"', "\u00bb": '"', "\uff02": '"',
    # ellipsis and bullets
    "\u2026": "...", "\u2022": "-", "\u2023": "-", "\u2043": "-", "\u2219": "-", "\u25cf": "-",
    "\u25e6": "-", "\u00b7": "-",
    # spaces
    "\u00a0": " ", "\u2002": " ", "\u2003": " ", "\u2004": " ", "\u2005": " ", "\u2006": " ",
    "\u2007": " ", "\u2008": " ", "\u2009": " ", "\u200a": " ", "\u202f": " ", "\u205f": " ",
    "\u3000": " ", "\t": " ",
    # zero-width (ZWJ and VS16 are handled per mode, they glue emoji sequences together)
    "\u200b": "", "\u200c": "", "\u2060": "", "\ufeff": "", "\u00ad": "",
    # marks
    "\u2122": "", "\u00ae": "", "\u00a9": "", "\u2120": "",
    # line separators
    "\u2028": "\n", "\u2029": "\n",
}
_EMOJI_GLUE = {"\u200d", "\ufe0f", "\ufe0e"}


def is_gsm7(text):
    return all(ch in GSM7_BASIC or ch in GSM7_EXT for ch in text)


def _fold(ch):
    """NFKD-fold one character to GSM-7 if that is lossless enough (accents, fullwidth, ligatures)."""
    if unicodedata.category(ch)[0] not in "LNPZ":
        return None
    folded = "".join(c for c in unicodedata.normalize("NFKD", ch) if not unicodedata.combining(c))
    folded = "".join(REPLACEMENTS.get(c, c) for c in folded)
    return folded if folded and is_gsm7(folded) else None


def sanitize(text, mode="public"):
    """Return the SMS-safe body. mode is "public" (GSM-7 only) or "private" (emoji kept)."""
    if mode not in ("public", "private"):
        raise ValueError(mode)
    out = []
    for ch in text or "":
        if ch in GSM7_BASIC or ch in GSM7_EXT:
            out.append(ch)
            continue
        if ch in REPLACEMENTS:
            out.append(REPLACEMENTS[ch])
            continue
        if ch in _EMOJI_GLUE:
            if mode == "private":
                out.append(ch)
            continue
        folded = _fold(ch)
        if folded is not None:
            out.append(folded)
        elif mode == "private":
            out.append(ch)
    body = "".join(out).replace("\r\n", "\n").replace("\r", "\n")
    body = re.sub(r"[ ]{2,}", " ", body)
    body = re.sub(r" *\n *", "\n", body)
    body = re.sub(r"\n{3,}", "\n\n", body)
    return body.strip()


def encoding_of(text):
    return "GSM-7" if is_gsm7(text) else "UCS-2"


def units(text):
    """Length in the units carriers bill: GSM-7 septets or UCS-2 (UTF-16) code units."""
    if is_gsm7(text):
        return sum(2 if ch in GSM7_EXT else 1 for ch in text)
    return len(text.encode("utf-16-le")) // 2


def segments(text):
    n = units(text)
    if is_gsm7(text):
        return 1 if n <= 160 else math.ceil(n / 153)
    return 1 if n <= 70 else math.ceil(n / 67)


def stats(text):
    return {"encoding": encoding_of(text), "units": units(text), "chars": len(text), "segments": segments(text)}
