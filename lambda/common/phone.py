"""US phone validation (stdlib only, no libphonenumber).

+1 also covers Canada and ~20 Caribbean and Pacific countries, which are classic
SMS-pumping destinations, so only area codes assigned to the 50 states and DC
are accepted. Toll-free, premium (900), and personal-service (5XX) codes are
never in the allowlist.
"""

import re

US_AREA_CODES = frozenset("""
201 202 203 205 206 207 208 209 210 212 213 214 215 216 217 218 219 220 223 224
225 227 228 229 231 234 235 239 240 248 251 252 253 254 256 260 262 267 269 270
272 274 276 279 281 283 301 302 303 304 305 307 308 309 310 312 313 314 315 316
317 318 319 320 321 323 324 325 326 327 329 330 331 332 334 336 337 339 341 346
347 350 351 352 353 357 360 361 363 364 369 380 385 386 401 402 404 405 406 407
408 409 410 412 413 414 415 417 419 423 424 425 430 432 434 435 436 440 442 443
445 447 448 458 463 464 469 470 471 472 475 478 479 480 484 501 502 503 504 505
507 508 509 510 512 513 515 516 517 518 520 530 531 534 539 540 541 551 557 559
561 562 563 564 567 570 571 572 573 574 575 580 582 585 586 601 602 603 605 606
607 608 609 610 612 614 615 616 617 618 619 620 623 624 626 628 629 630 631 636
640 641 645 646 650 651 656 657 659 660 661 662 667 669 678 679 680 681 682 686
689 701 702 703 704 706 707 708 712 713 714 715 716 717 718 719 720 724 725 726
727 728 730 731 732 734 737 740 743 747 754 757 760 762 763 765 769 770 771 772
773 774 775 779 781 785 786 801 802 803 804 805 806 808 810 812 813 814 815 816
817 818 820 821 826 828 830 831 832 835 838 839 840 843 845 847 848 850 854 856
857 858 859 860 861 862 863 864 865 870 872 878 901 903 904 906 907 908 909 910
912 913 914 915 916 917 918 919 920 925 928 929 930 931 934 936 937 938 940 941
943 945 947 948 949 951 952 954 956 959 970 971 972 973 975 978 979 980 983 984
985 986 989
""".split())

_NANP = re.compile(r"^\+1([2-9]\d{2})([2-9]\d{2})(\d{4})$")
_ALLOWED_CHARS = re.compile(r"^[\d\s().+\-]{7,25}$")


def normalize_us(raw, test_numbers=()):
    """Return the E.164 form of a US number, or None if it isn't acceptable."""
    if not isinstance(raw, str):
        return None
    raw = raw.strip()
    if not _ALLOWED_CHARS.match(raw):
        return None
    if raw.count("+") > 1 or ("+" in raw and not raw.startswith("+")):
        return None
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 10:
        digits = "1" + digits
    if len(digits) != 11 or not digits.startswith("1"):
        return None
    e164 = "+" + digits
    if e164 in test_numbers:
        return e164
    m = _NANP.match(e164)
    if not m:
        return None
    area, exchange, line = m.groups()
    if area not in US_AREA_CODES:
        return None
    if exchange[1:] == "11":
        return None
    if exchange == "555" and line.startswith("01"):
        return None
    return e164


def mask(e164):
    """+15005550006 -> +1******0006 (for logs)."""
    if not e164 or len(e164) < 6:
        return "***"
    return e164[:2] + "*" * (len(e164) - 6) + e164[-4:]
