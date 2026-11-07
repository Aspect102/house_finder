"""Rightmove adapter.

Rightmove's search page is a Next.js app that embeds results in
``__NEXT_DATA__`` under ``props.pageProps.searchResults.properties``. We
resolve the location via Rightmove's public typeahead endpoint, then parse
that payload. Falls back to the headless browser if needed.
"""

from __future__ import annotations

import json
import logging
import re
from urllib.parse import quote_plus

from ..normalize import (
    detect_area,
    detect_bills_included,
    detect_furnished,
    parse_beds,
    parse_postcode,
    parse_price,
)
from ..schemas import RawListing, SearchQuery
from .base import BaseSource, SourceError
from .htmlutil import extract_json_ld, strip_tags

log = logging.getLogger(__name__)

_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.DOTALL
)
_TYPEAHEAD_URL = "https://los.rightmove.co.uk/typeahead"


class RightmoveSource(BaseSource):
    name = "rightmove"
    base_url = "https://www.rightmove.co.uk"

    def __init__(self) -> None:
        super().__init__()
        self._location_cache: dict[str, str] = {}

    # ---- location resolution ---------------------------------------
    def resolve_location(self, location: str) -> str:
        """Return a Rightmove locationIdentifier (e.g. ``REGION^116``)."""
        key = location.strip().lower()
        if key in self._location_cache:
            return self._location_cache[key]
        query = location.split(",")[0].strip()
        try:
            data = self.get_json(_TYPEAHEAD_URL, params={"query": query, "limit": 5})
        except SourceError:
            data = None
        identifier = ""
        if isinstance(data, dict):
            for match in data.get("matches", []):
                if match.get("type") in ("REGION", "OUTCODE", "POSTCODE", "PLACE"):
                    identifier = f"{match['type']}^{match['id']}"
                    break
        if not identifier:
            # Sensible default: Bath, Somerset.
            identifier = "REGION^116"
        self._location_cache[key] = identifier
        return identifier

    def search_url(self, query: SearchQuery, *, index: int = 0) -> str:
        identifier = self.resolve_location(query.location)
        parts = [
            f"{self.base_url}/property-to-rent/find.html",
            f"?searchLocation={quote_plus(query.location)}",
            "&useLocationIdentifier=true",
            f"&locationIdentifier={identifier.replace('^', '%5E')}",
            f"&minBedrooms={query.min_beds or ''}",
            f"&maxBedrooms={query.max_beds or ''}",
            f"&minPrice={query.min_price or ''}",
            f"&maxPrice={query.max_price or ''}",
            f"&radius={query.radius_miles}",
            "&includeLetAgreed=false",
        ]
        if index:
            parts.append(f"&index={index}")
        return "".join(parts)

    def fetch(self, query: SearchQuery) -> list[RawListing]:
        out: list[RawListing] = []
        seen_ids: set[str] = set()
        index = 0
        for _ in range(max(1, self.settings.max_pages_per_source)):
            if len(out) >= query.max_results:
                break
            url = self.search_url(query, index=index)
            try:
                html = self.get_text(url)
            except SourceError:
                if not self.settings.use_playwright_fallback:
                    raise
                log.info("rightmove: HTTP failed, trying browser")
                html = self.get_html_with_browser(
                    url, wait_selector='[data-testid="propertyCard"]'
                )

            page = self._parse_next_data(html)
            if not page:
                page = self._parse_json_ld(html)
            if not page:
                break
            added = 0
            for item in page:
                if item.source_id in seen_ids:
                    continue
                seen_ids.add(item.source_id)
                out.append(item)
                added += 1
            # Rightmove serves 25 results per page; stop at the final page.
            if added == 0 or len(page) < 24:
                break
            index += 24
        return out[: query.max_results]

    # ---- parsers ----------------------------------------------------
    def _parse_next_data(self, html: str) -> list[RawListing]:
        m = _NEXT_DATA_RE.search(html)
        if not m:
            return []
        try:
            data = json.loads(m.group(1))
        except ValueError:
            return []
        props = (
            data.get("props", {})
            .get("pageProps", {})
            .get("searchResults", {})
            .get("properties", [])
        )
        out: list[RawListing] = []
        for p in props:
            try:
                out.append(self._from_property(p))
            except Exception:  # pragma: no cover - defensive
                log.debug("rightmove: skipping malformed property", exc_info=True)
        return out

    def _from_property(self, p: dict) -> RawListing:
        pid = str(p.get("id") or p.get("propertyId") or "")
        price = p.get("price") or {}
        price_pcm = None
        if isinstance(price, dict):
            amount = price.get("amount")
            freq = (price.get("frequency") or "monthly").lower()
            if amount:
                price_pcm = int(amount)
                if freq.startswith("week"):
                    price_pcm = int(amount * 52 / 12)
        elif price is not None:
            price_pcm = parse_price(str(price))

        property_type = str(p.get("propertySubType") or "").strip()
        address = p.get("displayAddress") or ""
        beds = p.get("bedrooms")
        if beds is None:
            beds = parse_beds(property_type)
        # "House Share" listings are individual rooms inside a shared house,
        # priced per room. Treating them as the whole 6-bed property would
        # put a ~£900 room next to genuine £5k houses in price sorts.
        is_house_share = property_type.lower() == "house share"
        if is_house_share:
            room_title = f"Room in a {beds} bed house share" if beds else "Room in a house share"
            room_beds = 1
            title = room_title
        else:
            room_beds = int(beds) if beds is not None else None
            if beds and property_type:
                title = f"{beds} bed {property_type.lower()}"
            else:
                title = property_type or "Property"
        summary = strip_tags(str(p.get("summary") or ""))
        text = f"{title} {address} {summary}"

        images = p.get("images") or []
        image_url = ""
        if images and isinstance(images, list) and isinstance(images[0], dict):
            image_url = images[0].get("srcUrl") or ""

        location = p.get("location") or {}
        available = str(p.get("letAvailableDate") or "")
        # ISO timestamp -> ISO date; leave human text untouched for the parser.
        if "T" in available:
            available = available.split("T", 1)[0]

        property_url = str(p.get("propertyUrl") or f"/properties/{pid}")
        property_url = property_url.split("#", 1)[0]

        return RawListing(
            source=self.name,
            source_id=pid,
            url=self.absolute(property_url),
            title=title,
            price_pcm=price_pcm,
            beds=room_beds,
            baths=p.get("bathrooms"),
            property_type=("Room in house share" if is_house_share else property_type),
            address=address,
            area=detect_area(address),
            postcode=parse_postcode(address),
            latitude=location.get("latitude") if isinstance(location, dict) else None,
            longitude=location.get("longitude") if isinstance(location, dict) else None,
            furnished=detect_furnished(text),
            bills_included=detect_bills_included(text),
            available_from=available,
            tenancy_start=available,
            description=summary,
            image_url=image_url,
            raw=p,
        )

    def _parse_json_ld(self, html: str) -> list[RawListing]:
        out: list[RawListing] = []
        for blob in extract_json_ld(html):
            items = blob if isinstance(blob, list) else [blob]
            for item in items:
                if not isinstance(item, dict):
                    continue
                if item.get("@type") not in ("Product", "Residence", "Apartment", "House", "Offer"):
                    continue
                try:
                    out.append(self._from_ld(item))
                except Exception:  # pragma: no cover
                    continue
        return out

    def _from_ld(self, item: dict) -> RawListing:
        url = item.get("url") or ""
        pid = url.rstrip("/").split("/")[-1] or item.get("name", "")
        offers = item.get("offers") or {}
        price = offers.get("price") if isinstance(offers, dict) else None
        return RawListing(
            source=self.name,
            source_id=str(pid),
            url=url or self.base_url,
            title=str(item.get("name") or "Property"),
            price_pcm=int(float(price)) if price else None,
            beds=parse_beds(str(item.get("name", ""))),
            address=str(item.get("address") or ""),
            area=detect_area(str(item.get("address") or "")),
            postcode=parse_postcode(str(item.get("address") or "")),
            description=strip_tags(str(item.get("description") or "")),
            raw=item,
        )
