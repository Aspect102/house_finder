"""OpenRent adapter.

OpenRent renders server-side property cards (``class="pli search-property-card"``)
with the price, address and features as text. We parse those cards directly.
"""

from __future__ import annotations

import html as html_lib
import logging
import re

from ..normalize import (
    detect_area,
    detect_bills_included,
    detect_furnished,
    is_available_now,
    parse_beds,
    parse_postcode,
    parse_price,
)
from ..schemas import RawListing, SearchQuery
from .base import BaseSource, SourceBlocked, SourceError

log = logging.getLogger(__name__)

_CARD_RE = re.compile(
    r'<a href="(/property-to-rent/[^"]+)"[^>]*class="pli search-property-card',
    re.IGNORECASE,
)
_HREF_RE = re.compile(r'href="(/property-to-rent/[^"]+)"')
_ID_RE = re.compile(r'data-listing-id="(\d+)"')
_IMG_RE = re.compile(r'<img[^>]+src="([^"]+)"')
_MONTH_RE = re.compile(r"£([\d,]+)\s*(?:pcm|per\s*month)", re.IGNORECASE)
_WEEK_RE = re.compile(r"£([\d,]+)\s*(?:pw|per\s*week)", re.IGNORECASE)
# "£780 per person per month" / "£180 per person per week"
_PER_PERSON_RE = re.compile(
    r"£([\d,]+)\s*(?:per\s*person|pp)\s*(?:per\s*(week|month)|pcm|pw)",
    re.IGNORECASE,
)
_AVAIL_RE = re.compile(
    r"(available\s+(?:from|now|immediately|on)[^<.]{0,40}|available\s+immediately)",
    re.IGNORECASE,
)
_TAG_RE = re.compile(r"<[^>]+>")

_BLOCK_MARKERS = (
    "human verification",
    "captcha",
    "just a moment",
    "cf-challenge",
    "attention required",
    "access denied",
    "are you a human",
    "confirm you are human",
    "verify you are human",
    "awswaf",
    "aws waf",
    "challenge-platform",
    "checking your browser",
)


def _looks_blocked(html: str) -> bool:
    """True when the page is an anti-bot challenge rather than results."""
    low = (html or "").lower()
    return any(marker in low for marker in _BLOCK_MARKERS)


class OpenRentSource(BaseSource):
    name = "openrent"
    base_url = "https://www.openrent.co.uk"

    def search_url(self, query: SearchQuery, *, skip: int = 0) -> str:
        parts = [
            f"{self.base_url}/properties-to-rent/bath",
            "?term=Bath",
            f"&prices_min={query.min_price or ''}",
            f"&prices_max={query.max_price or ''}",
            f"&bedrooms_min={query.min_beds or ''}",
            f"&bedrooms_max={query.max_beds or ''}",
        ]
        if skip:
            parts.append(f"&skip={skip}")
        return "".join(parts)

    def fetch(self, query: SearchQuery) -> list[RawListing]:
        """OpenRent renders 20 cards per page and applies bed/price filters
        client-side, so scan pages and filter the parsed cards ourselves."""
        out: list[RawListing] = []
        seen_ids: set[str] = set()
        skip = 0
        for _ in range(max(1, self.settings.max_pages_per_source)):
            if len(out) >= query.max_results:
                break
            url = self.search_url(query, skip=skip)
            try:
                html = self.get_text(url)
                if not html.strip():
                    raise SourceError("empty response")
            except SourceError:
                if not self.settings.use_playwright_fallback:
                    raise
                log.info("openrent: HTTP failed/empty, trying browser")
                html = self.get_html_with_browser(url, wait_selector=".search-property-card")
            if not html.strip() or _looks_blocked(html):
                raise SourceBlocked(
                    "OpenRent is challenging this host (Cloudflare 'confirm you are human' "
                    "or an empty response); the exit IP is blocked"
                )
            page = self._parse_cards(html)
            if not page:
                break
            for item in page:
                if item.source_id in seen_ids:
                    continue
                # OpenRent applies filters client-side. If the bed count is
                # unknown (studio/room), it cannot satisfy a minimum-bed query.
                if query.min_beds and (item.beds is None or item.beds < query.min_beds):
                    continue
                if query.max_beds and item.beds is not None and item.beds > query.max_beds:
                    continue
                if query.min_price and item.price_pcm is not None and item.price_pcm < query.min_price:
                    continue
                if query.max_price and item.price_pcm is not None and item.price_pcm > query.max_price:
                    continue
                seen_ids.add(item.source_id)
                out.append(item)
            # 20 results per page; the final page is shorter.
            if len(page) < 20:
                break
            skip += 20
        return out[: query.max_results]

    def _parse_cards(self, html: str) -> list[RawListing]:
        out: list[RawListing] = []
        matches = list(_CARD_RE.finditer(html))
        for i, m in enumerate(matches):
            start = m.start()
            end = matches[i + 1].start() if i + 1 < len(matches) else min(len(html), start + 8000)
            card = html[start:end]
            try:
                listing = self._from_card(card)
            except Exception:  # pragma: no cover
                continue
            if listing:
                out.append(listing)
        return out

    def _from_card(self, card: str) -> RawListing | None:
        href_m = _HREF_RE.search(card)
        if not href_m:
            return None
        href = href_m.group(1)
        id_m = _ID_RE.search(card)
        pid = id_m.group(1) if id_m else href.rstrip("/").split("/")[-1]

        text = html_lib.unescape(_TAG_RE.sub(" ", card))
        text = re.sub(r"\s+", " ", text).strip()
        low_text = text.lower()

        # The first image alt is "6 Bed Terraced House, Brook Road, BA2".
        full_title = ""
        img_m = _IMG_RE.search(card)
        if img_m:
            alt_m = re.search(r'alt="([^"]+)"', card)
            if alt_m:
                full_title = html_lib.unescape(alt_m.group(1)).strip()
        if not full_title:
            title_m = re.search(r"<h3[^>]*>(.*?)</h3>", card, re.DOTALL)
            full_title = (
                html_lib.unescape(_TAG_RE.sub("", title_m.group(1))).strip()
                if title_m
                else ""
            )

        title_parts = [p.strip() for p in full_title.split(",") if p.strip()]
        address = ", ".join(title_parts[1:]) if len(title_parts) > 1 else (full_title or "")
        beds = parse_beds(full_title)
        if beds is None and "room" not in full_title.lower() and "studio" not in full_title.lower():
            beds = parse_beds(text)
        is_room = full_title.lower().startswith("room in") or "studio" in full_title.lower()
        if is_room:
            beds = 1
        property_type = re.sub(r"^\d+\s*bed\s*", "", title_parts[0], flags=re.I) if title_parts else ""
        if is_room:
            property_type = "Room in shared house" if "studio" not in full_title.lower() else "Studio"

        price_pcm = None
        per_person = "per person" in low_text or " pp " in low_text or "pp pcm" in low_text
        pp_m = _PER_PERSON_RE.search(text)
        if pp_m:
            amount = int(pp_m.group(1).replace(",", ""))
            if (pp_m.group(2) or "month").lower().startswith("w"):
                amount = int(amount * 52 / 12)
            price_pcm = amount * beds if beds else amount
        if price_pcm is None:
            m = _MONTH_RE.search(text)
            if m:
                price_pcm = int(m.group(1).replace(",", ""))
                if per_person and beds:
                    price_pcm *= beds
            else:
                m = _WEEK_RE.search(text)
                if m:
                    price_pcm = int(int(m.group(1).replace(",", "")) * 52 / 12)
                    if per_person and beds:
                        price_pcm *= beds
        if price_pcm is None and "£" in text:
            # Some cards simply show "£2,400" with no suffix.
            price_pcm = parse_price(text)
        # Student cards often quote a per-person rent without spelling out
        # "per person"; a sub-£1k price for a 3+ bed student house is one.
        if (
            price_pcm
            and beds
            and beds >= 3
            and price_pcm < 1000
            and "student" in low_text
            and not per_person
        ):
            price_pcm *= beds

        avail_m = _AVAIL_RE.search(text)
        available_from = avail_m.group(1).strip() if avail_m else ""
        available_now = is_available_now(available_from)

        return RawListing(
            source=self.name,
            source_id=str(pid),
            url=self.absolute(href),
            title=full_title or "Property",
            price_pcm=price_pcm,
            beds=beds,
            property_type=property_type,
            address=address,
            area=detect_area(address),
            postcode=parse_postcode(address),
            furnished=detect_furnished(text),
            bills_included=detect_bills_included(text),
            available_from=available_from,
            available_now=available_now if available_from else None,
            description=text[:800],
            image_url=("https:" + img_m.group(1)) if img_m and img_m.group(1).startswith("//") else (img_m.group(1) if img_m else ""),
            raw={"href": href},
        )
