"""Zoopla adapter.

Zoopla blocks plain HTTP (403) and renders results client-side, so we use the
headless browser and parse the rendered listing cards
(``data-testid="listing-card-content"``).
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
from .base import BaseSource, SourceError

log = logging.getLogger(__name__)

_CARD_RE = re.compile(r'data-testid="listing-card-content"')
_DETAIL_RE = re.compile(r'href="(/to-rent/details/(\d+)/)"')
_PRICE_RE = re.compile(r"£([\d,]+)\s*pcm", re.IGNORECASE)
_BEDS_RE = re.compile(r">(\d+)\s*beds?<", re.IGNORECASE)
_BATHS_RE = re.compile(r">(\d+)\s*baths?<", re.IGNORECASE)
_ADDR_RE = re.compile(r'<address[^>]*>(.*?)</address>', re.DOTALL)
_IMG_RE = re.compile(r'<img[^>]+src="([^"]+)"')
_AVAIL_RE = re.compile(
    r"(available\s+(?:from|now|immediately)[^<.]{0,40})", re.IGNORECASE
)
_TAG_RE = re.compile(r"<[^>]+>")


class ZooplaSource(BaseSource):
    name = "zoopla"
    base_url = "https://www.zoopla.co.uk"
    needs_browser = True

    def search_url(self, query: SearchQuery) -> str:
        return (
            f"{self.base_url}/to-rent/property/bath/"
            f"?beds_min={query.min_beds}"
            f"&beds_max={query.max_beds or ''}"
            f"&price_frequency=per_month"
            f"&price_min={query.min_price or ''}"
            f"&price_max={query.max_price or ''}"
            f"&radius={query.radius_miles}"
            f"&q=Bath%2C%20Somerset"
        )

    def fetch(self, query: SearchQuery) -> list[RawListing]:
        url = self.search_url(query)
        html = ""
        # Zoopla returns 403 to plain HTTP; go straight to the browser.
        try:
            html = self.get_html_with_browser(
                url, wait_selector='[data-testid="listing-card-content"]'
            )
        except SourceError:
            log.info("zoopla: browser failed, trying HTTP")
            html = self.get_text(url)
        return self._parse_cards(html)[: query.max_results]

    def _parse_cards(self, html: str) -> list[RawListing]:
        out: list[RawListing] = []
        seen_ids: set[str] = set()
        matches = list(_CARD_RE.finditer(html))
        for i, m in enumerate(matches):
            start = m.start()
            end = matches[i + 1].start() if i + 1 < len(matches) else min(len(html), start + 6000)
            card = html[start:end]
            try:
                listing = self._from_card(card)
            except Exception:  # pragma: no cover
                continue
            if listing and listing.source_id not in seen_ids:
                seen_ids.add(listing.source_id)
                out.append(listing)
        return out

    def _from_card(self, card: str) -> RawListing | None:
        detail = _DETAIL_RE.search(card)
        if not detail:
            return None
        href, pid = detail.group(1), detail.group(2)

        price_m = _PRICE_RE.search(card)
        price_pcm = int(price_m.group(1).replace(",", "")) if price_m else None

        beds_m = _BEDS_RE.search(card)
        baths_m = _BATHS_RE.search(card)
        addr_m = _ADDR_RE.search(card)
        address = html_lib.unescape(_TAG_RE.sub("", addr_m.group(1))).strip() if addr_m else ""

        img_m = _IMG_RE.search(card)
        image_url = img_m.group(1) if img_m else ""

        text = html_lib.unescape(_TAG_RE.sub(" ", card))
        text = re.sub(r"\s+", " ", text).strip()
        avail_m = _AVAIL_RE.search(text)
        available_from = avail_m.group(1).strip() if avail_m else ""
        beds = int(beds_m.group(1)) if beds_m else parse_beds(address)
        return RawListing(
            source=self.name,
            source_id=pid,
            url=self.absolute(href),
            title=address or (f"{beds} bed property" if beds else "Property"),
            price_pcm=price_pcm,
            beds=beds,
            baths=int(baths_m.group(1)) if baths_m else None,
            address=address,
            area=detect_area(address),
            postcode=parse_postcode(address),
            furnished=detect_furnished(text),
            bills_included=detect_bills_included(text),
            available_from=available_from,
            available_now=is_available_now(available_from) if available_from else None,
            description=text[:800],
            image_url=image_url,
            raw={"href": href},
        )
