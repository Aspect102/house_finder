"""Normalisation helpers: turn messy scraped values into clean fields."""

from __future__ import annotations

import hashlib
import re
from datetime import date

_PRICE_RE = re.compile(r"([\d,]+(?:\.\d+)?)")
_BEDS_RE = re.compile(r"(\d+)\s*(?:bed|bedroom|double|room)", re.IGNORECASE)
_POSTCODE_RE = re.compile(r"\b([A-Z]{1,2}\d{1,2}[A-Z]?)\s*(\d[A-Z]{2})\b", re.IGNORECASE)
_OUTCODE_RE = re.compile(r"\b([A-Z]{1,2}\d{1,2}[A-Z]?)\b(?!\s*\d[A-Z]{2})", re.IGNORECASE)

# Date parsing for "available from" / tenancy start dates.
_ISO_DATE_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})")
_ISO_MONTH_RE = re.compile(r"\b(\d{4})-(\d{2})\b")
_UK_DATE_RE = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b")
_WORD_DATE_RE = re.compile(
    r"\b(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]+)\s+(\d{4})\b", re.IGNORECASE
)
_MONTH_YEAR_RE = re.compile(r"\b([A-Za-z]+)\s+(\d{4})\b", re.IGNORECASE)
_MONTHS = {
    "jan": 1, "january": 1,
    "feb": 2, "february": 2,
    "mar": 3, "march": 3,
    "apr": 4, "april": 4,
    "may": 5,
    "jun": 6, "june": 6,
    "jul": 7, "july": 7,
    "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10,
    "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}
_NOW_WORDS = ("now", "asap", "immediately", "available now", "immediate")

# Bath neighbourhoods / areas we care about (for area tagging).
BATH_AREAS = [
    "City Centre",
    "Oldfield Park",
    "Twerton",
    "Combe Down",
    "Widcombe",
    "Lansdown",
    "Bear Flat",
    "Odd Down",
    "Weston",
    "Newbridge",
    "Bathwick",
    "Kingsmead",
    "Southdown",
    "Whiteway",
    "Foxhill",
    "Larkhall",
    "Bathampton",
    "Batheaston",
    "Bathford",
    "Saltford",
    "Midsomer Norton",
    "Radstock",
    "Keynsham",
    "Freshford",
    "Corston",
    "Newton St Loe",
]

FURNISHED_TRUE = ("furnished", "fully furnished", "part furnished")
FURNISHED_FALSE = ("unfurnished", "not furnished")
BILLS_TRUE = ("bills included", "bills inc", "all bills", "inclusive of bills", "bills inclusive")


def parse_price(text: str | None) -> int | None:
    """Extract an integer price (pounds) from free text."""
    if not text:
        return None
    m = _PRICE_RE.search(text.replace("£", ""))
    if not m:
        return None
    try:
        return int(float(m.group(1).replace(",", "")))
    except ValueError:
        return None


def parse_beds(text: str | None) -> int | None:
    if not text:
        return None
    m = _BEDS_RE.search(text)
    if m:
        return int(m.group(1))
    # Bare-number fallback only for short strings (e.g. "3 Bath"), to avoid
    # grabbing digits out of prices/addresses in long card text.
    if len(text) <= 30:
        m = re.search(r"\b(\d{1,2})\b", text)
        return int(m.group(1)) if m else None
    return None


def parse_postcode(text: str | None) -> str:
    """Return a full postcode if present, else the outcode (e.g. ``BA2``)."""
    if not text:
        return ""
    m = _POSTCODE_RE.search(text)
    if m:
        return f"{m.group(1).upper()} {m.group(2).upper()}"
    m = _OUTCODE_RE.search(text)
    if m:
        return m.group(1).upper()
    return ""


def detect_area(text: str | None) -> str:
    if not text:
        return ""
    low = text.lower()
    for area in BATH_AREAS:
        if area.lower() in low:
            return area
    return ""


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def parse_date(text: str | None) -> date | None:
    """Extract a tenancy/availability date from free text.

    Handles ISO (``2026-07-01``), ISO month (``2026-07``), UK (``01/07/2026``),
    worded (``1 July 2026``) and month-year (``July 2026``) forms. Month-year
    resolves to the first of the month. Returns ``None`` when no date is found.
    """
    if not text:
        return None
    text = text.strip()

    m = _ISO_DATE_RE.search(text)
    if m:
        return _safe_date(int(m.group(1)), int(m.group(2)), int(m.group(3)))

    m = _UK_DATE_RE.search(text)
    if m:
        return _safe_date(int(m.group(3)), int(m.group(2)), int(m.group(1)))

    m = _WORD_DATE_RE.search(text)
    if m:
        month = _MONTHS.get(m.group(2).lower())
        if month:
            return _safe_date(int(m.group(3)), month, int(m.group(1)))

    m = _ISO_MONTH_RE.search(text)
    if m:
        return _safe_date(int(m.group(1)), int(m.group(2)), 1)

    m = _MONTH_YEAR_RE.search(text)
    if m:
        month = _MONTHS.get(m.group(1).lower())
        if month:
            return _safe_date(int(m.group(2)), month, 1)

    return None


def is_available_now(text: str | None) -> bool:
    """True when the listing advertises immediate availability."""
    if not text:
        return False
    low = text.lower()
    return any(word in low for word in _NOW_WORDS)


def normalise_date(text: str | None) -> str:
    """Return an ISO ``YYYY-MM-DD`` string when a date can be parsed."""
    parsed = parse_date(text)
    return parsed.isoformat() if parsed else ""


def tenancy_start_iso(text: str | None) -> str:
    """Alias used by adapters for tenancy start dates."""
    return normalise_date(text)


def detect_furnished(text: str | None) -> bool | None:
    if not text:
        return None
    low = text.lower()
    if any(k in low for k in FURNISHED_FALSE):
        return False
    if any(k in low for k in FURNISHED_TRUE):
        return True
    return None


def detect_bills_included(text: str | None) -> bool | None:
    if not text:
        return None
    low = text.lower()
    if any(k in low for k in BILLS_TRUE):
        return True
    return None


def whole_property_price(
    price_pcm: int | None,
    beds: int | None,
    *,
    threshold: int = 250,
    max_per_person: int = 1200,
) -> int | None:
    """Return a whole-property monthly rent.

    Rightmove and OpenRent usually quote the whole house, but some portals
    (notably OnTheMarket/Zoopla for student lets) show the per-person monthly
    rent while labelling it as a whole-property price. Two signals identify
    that case:

    * the implied rent per bed is far below Bath student rents (default
      ``threshold`` £250/bed/month), and
    * the figure is in the per-person range (default ``max_per_person``
      £1,200/month); a genuine whole-house rent above that is left alone.

    Beds below three are treated as single rooms and are never multiplied.
    """
    if not price_pcm or not beds or beds < 3:
        return price_pcm
    if price_pcm <= max_per_person and price_pcm / beds < threshold:
        return int(price_pcm * beds)
    return price_pcm


def price_per_person_per_week(price_pcm: int | None, beds: int | None) -> float | None:
    """Convert monthly rent to per-person-per-week (standard student metric)."""
    if not price_pcm or not beds or beds <= 0:
        return None
    return round((price_pcm * 12 / 52) / beds, 2)


def content_hash(*parts: object) -> str:
    """Stable hash of the fields that matter for change detection."""
    joined = "|".join("" if p is None else str(p) for p in parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def normalise_postcode_key(postcode: str) -> str:
    return re.sub(r"\s+", "", postcode or "").upper()


def dedupe_key(source: str, source_id: str) -> str:
    return f"{source}:{source_id}"
