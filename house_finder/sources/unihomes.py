"""Unihomes adapter.

UniHomes embeds its full result set in the page as
``:initial-properties="JSON.parse('...')"``. We extract and parse that array.
Prices are quoted per room per week, so we convert to a whole-property
monthly figure (``price * bedrooms * 52 / 12``).
"""

from __future__ import annotations

import json
import logging
import re

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

log = logging.getLogger(__name__)

_PROPS_RE = re.compile(
    r":initial-properties=\"JSON\.parse\('(.*?)'\)\"", re.DOTALL
)


class UnihomesSource(BaseSource):
    name = "unihomes"
    base_url = "https://www.unihomes.co.uk"

    def search_url(self, query: SearchQuery, *, page: int = 1) -> str:
        parts = [
            f"{self.base_url}/student-accommodation/bath",
            f"?bedrooms={query.min_beds or ''}",
            f"&max_bedrooms={query.max_beds or ''}",
            f"&min_price={query.min_price or ''}",
            f"&max_price={query.max_price or ''}",
        ]
        if page > 1:
            parts.append(f"&page={page}")
        return "".join(parts)

    def fetch(self, query: SearchQuery) -> list[RawListing]:
        out: list[RawListing] = []
        seen_ids: set[str] = set()
        for page in range(1, max(1, self.settings.max_pages_per_source) + 1):
            if len(out) >= query.max_results:
                break
            url = self.search_url(query, page=page)
            try:
                html = self.get_text(url)
            except SourceError:
                if not self.settings.use_playwright_fallback:
                    raise
                log.info("unihomes: HTTP failed, trying browser")
                html = self.get_html_with_browser(url, wait_selector="[class*=property-card]")

            listings = self._parse_embedded(html)
            if not listings:
                listings = self._parse_loose(html)
            if not listings:
                break
            added = 0
            for item in listings:
                if item.source_id in seen_ids:
                    continue
                if query.min_beds and item.beds is not None and item.beds < query.min_beds:
                    continue
                if query.max_beds and item.beds is not None and item.beds > query.max_beds:
                    continue
                seen_ids.add(item.source_id)
                out.append(item)
                added += 1
            # UniHomes advertises whether another page exists; stop when it
            # doesn't (requesting page 3 after the end returns the full set).
            has_more = 'initial-properties-has-more-pages="true"' in html
            if not has_more or added == 0:
                break
        return out[: query.max_results]

    def _parse_embedded(self, html: str) -> list[RawListing]:
        m = _PROPS_RE.search(html)
        if not m:
            return []
        raw = m.group(1)
        try:
            # The attribute is a JS string containing JSON with \u0022 quotes.
            data = json.loads(raw.replace("\\u0022", '"'))
        except ValueError:
            try:
                data = json.loads(raw.encode("utf-8").decode("unicode_escape"))
            except (ValueError, UnicodeDecodeError):
                return []
        if not isinstance(data, list):
            return []
        out: list[RawListing] = []
        for p in data:
            try:
                out.append(self._from_property(p))
            except Exception:  # pragma: no cover
                continue
        return out

    def _from_property(self, p: dict) -> RawListing:
        pid = str(p.get("id") or "")
        beds = p.get("bedroomCount") or parse_beds(str(p.get("propertyName", "")))
        # price is per room per week.
        room_pw = parse_price(str(p.get("price") or p.get("cheapestRoomPrice") or ""))
        price_pcm = None
        if room_pw and beds:
            price_pcm = int(room_pw * beds * 52 / 12)
        elif room_pw:
            price_pcm = int(room_pw * 52 / 12)

        address = p.get("addressShort") or p.get("street") or ""
        title = p.get("propertyName") or "Student property"
        desc = p.get("shortDescription") or p.get("description") or ""
        text = f"{title} {address} {desc}"

        path = str(p.get("path") or "").replace("\\/", "/")
        url = path or f"{self.base_url}/property/{pid}"
        image = str(p.get("mainImage") or "").replace("\\/", "/")

        # Addresses are often abbreviated ("Winchester Road, Oldfield Park");
        # the URL slug also contains the neighbourhood, so use both for tags.
        area = detect_area(str(address)) or detect_area(path.replace("-", " "))
        postcode = p.get("postcode") or parse_postcode(str(address))

        return RawListing(
            source=self.name,
            source_id=pid,
            url=url,
            title=str(title),
            price_pcm=price_pcm,
            beds=int(beds) if beds else None,
            baths=p.get("bathroomCount"),
            property_type=str(p.get("typeString") or "Student house"),
            address=str(address),
            area=area,
            postcode=postcode,
            latitude=p.get("latitude"),
            longitude=p.get("longitude"),
            # UniHomes is all-inclusive by definition.
            furnished=True,
            bills_included=True,
            available_from=str(p.get("availableFromShort") or p.get("availableFrom") or ""),
            tenancy_start=str(p.get("availableFrom") or p.get("availableFromShort") or ""),
            description=str(desc),
            image_url=image,
            raw=p,
        )

    def _parse_loose(self, html: str) -> list[RawListing]:
        from .htmlutil import extract_json_ld

        out: list[RawListing] = []
        for blob in extract_json_ld(html):
            items = blob if isinstance(blob, list) else [blob]
            for item in items:
                if isinstance(item, dict) and item.get("url"):
                    offers = item.get("offers")
                    price = parse_price(str(offers.get("price", ""))) if isinstance(offers, dict) else None
                    out.append(
                        RawListing(
                            source=self.name,
                            source_id=str(item.get("url")).rstrip("/").split("/")[-1],
                            url=str(item.get("url")),
                            title=str(item.get("name") or "Student property"),
                            price_pcm=price,
                            address=str(item.get("address") or ""),
                            area=detect_area(str(item.get("address") or "")),
                            raw=item,
                        )
                    )
        return out
