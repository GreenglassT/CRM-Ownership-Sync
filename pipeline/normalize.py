"""Normalization helpers shared by the scraper and the matcher.

Everything here is deterministic and dependency-free so the matching can be
explained field-by-field to a reviewer.
"""
import re
from functools import lru_cache

# USPS-style suffix / directional abbreviations. Keys are lowercase full words.
_STREET_WORDS = {
    "street": "st", "avenue": "ave", "road": "rd", "drive": "dr", "boulevard": "blvd",
    "lane": "ln", "court": "ct", "place": "pl", "highway": "hwy", "parkway": "pkwy",
    "circle": "cir", "terrace": "ter", "trail": "trl", "way": "way", "pike": "pike",
    "square": "sq", "north": "n", "south": "s", "east": "e", "west": "w",
    "northwest": "nw", "northeast": "ne", "southwest": "sw", "southeast": "se",
    "saint": "st", "mount": "mt", "fort": "ft",
    # common short forms seen in hand-entered CRM addresses
    "pk": "pike", "av": "ave", "avn": "ave", "tpke": "tpke", "turnpike": "tpke", "rte": "rte", "route": "rte",
    "expy": "expy", "expressway": "expy", "xing": "xing", "crossing": "xing", "trce": "trce", "trace": "trce",
    "crk": "crk", "creek": "crk",
}
_UNIT_WORDS = {"suite", "ste", "unit", "apt", "bldg", "building", "floor", "fl", "#"}

# Website marketing labels -> CRM care_type vocabulary.
CARE_MAP = {
    "short-term rehabilitation & nursing": "Skilled Nursing",
    "skilled nursing": "Skilled Nursing",
    "memory support": "Memory Care",
    "memory care": "Memory Care",
    "assisted living": "Assisted Living",
    "independent living": "Independent Living",
}
# When a location lists several offerings, the CRM holds one care_type. This is
# the order of precedence we use to pick the primary one (highest acuity first).
CARE_PRECEDENCE = ["Skilled Nursing", "Memory Care", "Assisted Living", "Independent Living"]

_NAME_STOP = {"the", "of", "at", "and", "&", "a", "an", "-", "–"}
# Words that describe *what* a facility is rather than *which* one. Dropped from
# name tokens so "Care Centre of Ashland" and "Health Care Center of Ashland" agree.
_NAME_GENERIC = {
    "care", "center", "centre", "health", "healthcare", "nursing", "rehab",
    "rehabilitation", "senior", "living", "community", "communities", "home",
    "manor", "village", "gardens", "estates", "campus", "commons", "place",
    "residence", "assisted", "memory", "skilled", "retirement",
}


@lru_cache(maxsize=None)
def norm_street(s: str) -> str:
    """'4850 Northwest Sylvania Avenue' -> '4850 nw sylvania ave'. Drops unit numbers."""
    s = (s or "").lower().replace(".", " ").replace(",", " ").replace("#", " # ")
    s = re.sub(r"[^a-z0-9 #]", " ", s)
    toks, out, skip = s.split(), [], False
    for t in toks:
        if skip:              # swallow the token after a unit word ("suite 200")
            skip = False
            continue
        if t in _UNIT_WORDS:
            skip = True
            continue
        out.append(_STREET_WORDS.get(t, t))
    return " ".join(out)


@lru_cache(maxsize=None)
def is_po_box(s: str) -> bool:
    return bool(re.match(r"^\s*p\.?\s*o\.?\s*box\b", (s or "").lower()))


@lru_cache(maxsize=None)
def street_number(s: str) -> str:
    m = re.match(r"^\s*(\d+)", s or "")
    return m.group(1) if m else ""


@lru_cache(maxsize=None)
def norm_phone(s: str) -> str:
    s = re.sub(r"(?i)\s*(x|ext\.?|extension)\s*\d+\s*$", "", s or "")   # drop extensions
    d = re.sub(r"\D", "", s)
    return d[-10:] if len(d) >= 10 else ""


@lru_cache(maxsize=None)
def norm_zip(s: str) -> str:
    return (s or "").strip()[:5]


@lru_cache(maxsize=None)
def norm_city(s: str) -> str:
    return re.sub(r"[^a-z ]", "", (s or "").lower()).strip()


@lru_cache(maxsize=None)
def name_tokens(s: str, drop_generic: bool = True) -> frozenset[str]:
    s = (s or "").lower().replace("&", " and ")
    toks = set(re.findall(r"[a-z0-9]+", s))
    toks -= _NAME_STOP
    if drop_generic:
        toks -= _NAME_GENERIC
    return frozenset(toks)


def jaccard(a: frozenset, b: frozenset) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def map_care(labels: list[str]) -> list[str]:
    """Map site labels to CRM vocabulary, de-duplicated, in precedence order."""
    mapped = {CARE_MAP.get(l.strip().lower(), l.strip()) for l in labels if l and l.strip()}
    return [c for c in CARE_PRECEDENCE if c in mapped] + sorted(mapped - set(CARE_PRECEDENCE))


def primary_care(labels: list[str]) -> str:
    m = map_care(labels)
    return m[0] if m else ""
