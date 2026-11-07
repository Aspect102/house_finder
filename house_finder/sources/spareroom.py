"""SpareRoom adapter.

SpareRoom is room-oriented (per-person) and sits behind an anti-bot captcha
for automated clients. We attempt HTTP, then the headless browser, and parse
the standard ``listing-result`` cards when they are served. If a captcha is
detected we raise a clear :class:`SourceError` so the sync reports it as a
partial failure rather than silently returning nothing.
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

_CARD_RE = re.compile(r'class="[^"]*listing-result[^"]*"', re.IGNORECASE)
_LI_RE = re.compile(r'<li\s+class="[^"]*listing-result[^"]*"([^>]*)>', re.IGNORECASE)
_ATTR_RE = re.compile(r'data-listing-([a-z0-9-]+)="([^"]*)"', re.IGNORECASE)
_DETAIL_RE = re.compile(r'href="(/flatshare/flatshare_detail\.pl\?flatshare_id=(\d+)[^"]*)"')
_WEEK_RE = re.compile(r"(pw|per week)", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")
_CAPTCHA_MARKERS = ("captcha", "are you a robot", "unusual traffic", "cf-challenge")


class SpareRoomSource(BaseSource):
    name = "spareroom"
    base_url = "https://www.spareroom.co.uk"

    def search_url(self, query: SearchQuery) -> str:
        # The public city page is server-rendered and does not require the
        # advanced-search POST that frequently trips the anti-bot challenge.
        return f"{self.base_url}/flatshare/bath"

    def fetch(self, query: SearchQuery) -> list[RawListing]:
        url = self.search_url(query)
        html = ""
        try:
            html = self.get_text(url)
        except SourceError:
            log.info("spareroom: HTTP failed, trying browser")
        if self._is_blocked(html):
            try:
                html = self.get_html_with_browser(url, wait_selector=".listing-result")
            except SourceError:
                pass
        if self._is_blocked(html):
            raise SourceError(
                "SpareRoom is serving an anti-bot challenge to automated clients; "
                "results cannot be fetched without manual browsing."
            )
        return self._parse_cards(html)[: query.max_results]

    @staticmethod
    def _is_blocked(html: str) -> bool:
        if not html:
            return True
        low = html.lower()
        has_cards = 'class="listing-result' in low or "listing-result" in low and "data-listing-id" in low
        if has_cards:
            return False
        if any(marker in low for marker in _CAPTCHA_MARKERS):
            return True
        return "flatshare_id=" not in html and "listing-result" not in low

    def _parse_cards(self, html: str) -> list[RawListing]:
        out: list[RawListing] = []
        seen_ids: set[str] = set()
        for m in _LI_RE.finditer(html):
            attrs = {
                key.lower(): html_lib.unescape(value)
                for key, value in _ATTR_RE.findall(m.group(1))
            }
            listing = self._from_attrs(attrs)
            if listing and listing.source_id not in seen_ids:
                seen_ids.add(listing.source_id)
                out.append(listing)
        if out:
            return out

        # Legacy markup fallback: parse detail links directly.
        for m in _DETAIL_RE.finditer(html):
            href, fid = m.group(1), m.group(2)
            window = html[m.end() : m.end() + 1200]
            listing = self._from_window(href, fid, window)
            if listing and listing.source_id not in seen_ids:
                seen_ids.add(listing.source_id)
                out.append(listing)
        return out

    def _from_attrs(self, attrs: dict[str, str]) -> RawListing | None:
        fid = attrs.get("id", "").strip()
        href = attrs.get("url", "").strip()
        if not fid or not href:
            return None
        title = attrs.get("title", "").strip() or "Room"
        address = attrs.get("neighbourhood", "").strip()
        postcode = attrs.get("postcode", "").strip()
        rooms = attrs.get("rooms-in-property", "").strip()
        prop_type = attrs.get("property-type", "").strip()
        rate = attrs.get("ad-headline-rate", "") or attrs.get("ad-rate-normalised", "")
        period = (
            attrs.get("ad-headline-rate-period", "")
            or attrs.get("ad-rate-normalised-period", "")
        ).lower()
        price = parse_price(html_lib.unescape(rate))
        weekly = period in {"pw", "w", "week", "weekly", "per week"} or period.endswith(
            "week"
        )
        if price and weekly:
            price_pcm = int(price * 52 / 12)
        else:
            price_pcm = price
        available_now = attrs.get("available-now", "").strip() in {"1", "true", "yes"}
        available_from = "Available now" if available_now else attrs.get("available-from", "").strip()
        description = " ".join(
            filter(
                None,
                [
                    title,
                    address,
                    postcode,
                    f"Room in a {rooms}-bed {prop_type}" if rooms else "",
                ],
            )
        )
        return RawListing(
            source=self.name,
            source_id=fid,
            url=self.absolute(href),
            title=title,
            price_pcm=price_pcm,
            # A SpareRoom listing is a single room unless the title says
            # otherwise; keep the property size in the type/description.
            beds=parse_beds(title) or 1,
            property_type=(
                f"Room in {rooms}-bed {prop_type}" if rooms else (prop_type or "Room")
            ),
            address=f"{address}, {postcode}".strip(", "),
            area=detect_area(address) or address,
            postcode=postcode or parse_postcode(address),
            furnished=detect_furnished(description),
            bills_included=detect_bills_included(description),
            available_from=available_from,
            available_now=available_now,
            description=description,
            raw=attrs,
        )

    def _from_window(self, href: str, fid: str, window: str) -> RawListing | None:
        text = html_lib.unescape(_TAG_RE.sub(" ", window))
        text = re.sub(r"\s+", " ", text).strip()
        price = parse_price(text)
        if price and _WEEK_RE.search(text):
            price = int(price * 52 / 12)
        title_m = re.search(r"<h[23][^>]*>(.*?)</h[23]>", window, re.DOTALL)
        title = html_lib.unescape(_TAG_RE.sub("", title_m.group(1))).strip() if title_m else "Room"
        return RawListing(
            source=self.name,
            source_id=fid,
            url=self.absolute(href),
            title=title or "Room",
            price_pcm=price,
            beds=parse_beds(title),
            address=title,
            area=detect_area(title),
            postcode=parse_postcode(text),
            furnished=detect_furnished(text),
            bills_included=detect_bills_included(text),
            description=text[:400],
            raw={"href": href},
        )
