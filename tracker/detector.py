"""Extract sponsor brand names from video descriptions.

Creators disclose sponsors in fairly regular language ("This video is
sponsored by NordVPN", "Thanks to Geekom for sponsoring"). We match those
phrases, then aggressively clean the captured text down to a plausible
brand name. Precision over recall: a missed sponsor is better than a
dashboard full of junk rows.

Only paid sponsorships count — a discount code, an affiliate link, or a
brand crediting a gifted/review product is a different, uninteresting
relationship for this tracker's purpose (agency outreach targets brands
actually paying for placement). So every pattern here requires actual
sponsor/partnership language; "use code X at Y" and "N% off X" style
disclosures are deliberately not matched, even though they're common and
easy to catch, because they signal affiliate/gifted deals at least as
often as paid ones.
"""
import re

# What a captured brand blob may run up to: sentence punctuation, newline,
# brackets, pipes, or emoji-ish separators.
_BLOB = r"([^\n.,;:!?()\[\]|•·—]{2,60})"

PATTERNS = [
    # "sponsored by X" / "Sponsor: X" / "this video is sponsored by X"
    re.compile(r"\bsponsored\s+by\s+" + _BLOB, re.I),
    re.compile(r"\bsponsor(?:\s+of\s+(?:this|today'?s)\s+video)?\s*:\s*" + _BLOB, re.I),
    # "thanks to X for sponsoring" — deliberately NOT "...for supporting": that
    # verb alone doesn't establish a paid relationship (gift/affiliate-only
    # mentions use it too), see the module docstring.
    re.compile(
        r"\bthanks?\s+(?:so\s+much\s+|again\s+|a\s+lot\s+)?to\s+(.{2,50}?)\s+for\s+sponsoring",
        re.I,
    ),
    re.compile(r"\bthank\s+you\s*,?\s+(?:to\s+)?(.{2,50}?)\s+for\s+sponsoring", re.I),
    # "brought to you by X"
    re.compile(r"\bbrought\s+to\s+you\s+by\s+" + _BLOB, re.I),
    # "in (paid) partnership with X" / "partnered with X" / "paid promotion by X"
    re.compile(r"\b(?:in\s+)?(?:a\s+)?(?:paid\s+)?partnership\s+with\s+" + _BLOB, re.I),
    re.compile(r"\bpartnered\s+with\s+" + _BLOB, re.I),
    re.compile(r"\bpaid\s+promotion\s+(?:by|from)\s+" + _BLOB, re.I),
    # "today's (video) sponsor is X"
    re.compile(r"\btoday'?s\s+(?:video\s+)?sponsor(?:\s+is)?\s*[,:]?\s+" + _BLOB, re.I),
]

# Sponsor-ish context words for the known-brand assist pass. Deliberately
# excludes discount/coupon/use-code/%-off/deal/offer/promo — those signal an
# affiliate or gifted-product relationship, not a paid sponsorship, and the
# owner doesn't want those tracked (2026-08-13: removed the standalone
# "use code X at Y" / "N% off X" PATTERNS entries for the same reason —
# a creator crediting a brand for a free product or an affiliate code isn't
# a sponsor, even though it reads like a disclosure).
_CONTEXT = re.compile(r"sponsor|partner|paid promotion|#ad\b|\bad\b", re.I)

# Words that never end a brand name — trimmed off the tail of a capture.
_TRAILING_STOP = {
    "for", "and", "at", "on", "in", "to", "the", "a", "an", "of", "with",
    "who", "that", "is", "are", "was", "were", "their", "its", "this",
    "today", "here", "more", "all", "my", "our", "your", "get", "use",
    "check", "out", "now", "over", "via", "from", "or", "so", "as", "by",
    "plan", "plans", "subscription", "order", "purchase", "membership", "deal",
}

# Captures that mean "the audience", not a brand.
_REJECT_SUBSTR = (
    "patreon", "patron", "member", "viewer", "subscrib", "supporter",
    "you guys", "everyone", "you all", "y'all", "my sponsor", "our sponsor",
    "no one", "nobody", "watching", "these companies", "the following",
    "link below", "links below", "description below", "the description",
    "season partner", "our partners", "our sponsors",
)
_REJECT_EXACT = {"me", "you", "us", "them", "all", "everybody", "yourself", "himself", "herself",
                 "http", "https", "www", "link", "links", "the link", "checkout", "check out",
                 "the checkout", "cart", "the cart"}


def brand_key(name):
    """Normalization used for grouping/dedup: lowercase alphanumerics only."""
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _clean(blob):
    s = blob.strip()
    # cut before any URL (even unspaced), common arrow/link lead-ins, and clause continuations
    s = re.split(r"https?://|https?$|\bwww\.|►|▶|→|<|\s@\s", s)[0]
    s = re.split(r"\s+[-–—]\s+", s)[0]
    s = re.split(
        r"\s+(?:for|who|which|where|because|get|use|using|save|grab|go|visit|click|try|sign|check|head|and|with)\s+",
        s,
    )[0]
    # drop possessive endings like "NordVPN's"
    s = re.sub(r"['’]s$", "", s.strip())
    s = re.sub(r"\s+", " ", s).strip(" \t-–—*_~'\"“”‘’.")
    words = s.split(" ")
    # trim leading fillers ("our friends at X", "the team at X") before capping
    while len(words) > 1 and words[0].lower() in {"our", "the", "my"} and words[1].lower() in {
        "friends", "team", "folks", "sponsor", "sponsors", "partner", "partners", "good"
    }:
        words = words[2:]
        while words and words[0].lower() in {"at", "over", "from"}:
            words = words[1:]
    # trim leading connectives so "with code NUTTY" reduces to "code NUTTY"
    while words and words[0].lower() in {"with", "using", "use", "via"}:
        words = words[1:]
    # cap at 4 words, then trim trailing stopwords
    words = words[:4]
    while words and words[-1].lower() in _TRAILING_STOP:
        words.pop()
    s = " ".join(words).strip()
    if len(s) < 2 or len(brand_key(s)) < 2:
        return None
    low = s.lower()
    if low in _REJECT_EXACT or any(t in low for t in _REJECT_SUBSTR):
        return None
    # "use code X at (desktop) checkout / at the cart" — a place, not a brand
    if low.split()[-1] in ("checkout", "cart"):
        return None
    # "code NUTTY" / "coupon SAVE20" — a discount code, not a brand
    if low.split()[0] in ("code", "coupon", "promo", "voucher"):
        return None
    if s.isdigit():
        return None
    return s


def detect_spoken(text, known_brands=()):
    """Detect sponsors in a transcript slice from inside a known sponsor segment.

    Same disclosure patterns as descriptions, but a known-brand mention needs
    no extra context — the SponsorBlock segment already establishes it.
    """
    if not text:
        return []
    found = {brand_key(b): (b, e) for b, e in detect_sponsors(text)}
    for name, key in known_brands:
        if key in found:
            continue
        m = re.search(r"(?<!\w)" + re.escape(name) + r"(?!\w)", text, re.I)
        if m:
            start = max(0, m.start() - 40)
            found[key] = (name, re.sub(r"\s+", " ", text[start : m.end() + 60]).strip())
    return list(found.values())


def detect_sponsors(text, known_brands=()):
    """Return [(brand, evidence)] found in a description. Deduped by brand_key.

    known_brands is an optional [(name, key)] list (the user's CRM). As a
    second pass, any known brand mentioned in the text within sponsor-ish
    context (sponsor/partner/code/% off/…) is picked up even when the
    disclosure phrasing doesn't match the patterns above.
    """
    if not text:
        return []
    found = {}
    for pat in PATTERNS:
        for m in pat.finditer(text):
            brand = _clean(m.group(1))
            if not brand:
                continue
            key = brand_key(brand)
            if key in found:
                continue
            start = max(0, m.start() - 30)
            evidence = re.sub(r"\s+", " ", text[start : m.end() + 30]).strip()
            found[key] = (brand, evidence)
    for name, key in known_brands:
        if key in found:
            continue
        m = re.search(r"(?<!\w)" + re.escape(name) + r"(?!\w)", text, re.I)
        if not m:
            continue
        window = text[max(0, m.start() - 120) : m.end() + 120]
        if _CONTEXT.search(window):
            start = max(0, m.start() - 40)
            evidence = re.sub(r"\s+", " ", text[start : m.end() + 60]).strip()
            found[key] = (name, evidence)
    return list(found.values())
