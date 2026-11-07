"""OnTheMarket adapter.

OnTheMarket is a Next.js app; results live in ``__NEXT_DATA__`` under
``props.pageProps.initialReduxState.results.list``. Falls back to the
headless browser if the payload is missing.
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
from .htmlutil import extract_json_ld, strip_tags

log = logging.getLogger(__name__)

_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.DOTALL
)


class OnTheMarketSource(BaseSource):
    name = "onthemarket"
    base_url = "https://www.onthemarket.com"

    def search_url(self, query: SearchQuery, *, page: int = 1) -> str:
        parts = [
            f"{self.base_url}/to-rent/property/bath/",
            f"?min-bedrooms={query.min_beds or ''}",
            f"&max-bedrooms={query.max_beds or ''}",
            f"&min-price={query.min_price or ''}",
            f"&max-price={query.max_price or ''}",
            f"&radius={query.radius_miles}",
        ]
        if page > 1:
            parts.append(f"&page={page}")
        return "".join(parts)

    def fetch(self, query: SearchQuery) -> list[RawListing]:
        out: list[RawListing] = []
        seen_ids: set[str] = set()
        for page_no in range(1, max(1, self.settings.max_pages_per_source) + 1):
            if len(out) >= query.max_results:
                break
            url = self.search_url(query, page=page_no)
            try:
                html = self.get_text(url)
            except SourceError:
                if not self.settings.use_playwright_fallback:
                    raise
                log.info("onthemarket: HTTP failed, trying browser")
                html = self.get_html_with_browser(url, wait_selector="[data-testid]")

            page = self._parse_next_data(html)
            if not page:
                page = self._parse_loose(html)
            if not page:
                break
            added = 0
            for item in page:
                if item.source_id in seen_ids:
                    continue
                seen_ids.add(item.source_id)
                out.append(item)
                added += 1
            # 30 results per page.
            if added == 0 or len(page) < 30:
                break
        return out[: query.max_results]

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
            .get("initialReduxState", {})
            .get("results", {})
            .get("list", [])
        )
        out: list[RawListing] = []
        for p in props:
            try:
                out.append(self._from_property(p))
            except Exception:  # pragma: no cover
                continue
        return out

    def _from_property(self, p: dict) -> RawListing:
        pid = str(p.get("id") or "")
        price_pcm = parse_price(str(p.get("short-price") or p.get("price") or ""))
        address = p.get("address") or ""
        title = p.get("property-title") or p.get("humanised-property-type") or "Property"
        features = p.get("features") or []
        text = " ".join([str(title), str(address), " ".join(map(str, features))])
        location = p.get("location") or {}
        cover = p.get("cover-image") or {}
        image_url = cover.get("default", "") if isinstance(cover, dict) else ""
        details = p.get("details-url") or f"/details/{pid}/"
        available = str(p.get("available-from") or "")
        if not available:
            for feature in features:
                low = str(feature).lower()
                if "available" in low:
                    available = str(feature)
                    break
        return RawListing(
            source=self.name,
            source_id=pid,
            url=self.absolute(details),
            title=str(title),
            price_pcm=price_pcm,
            beds=p.get("bedrooms") or parse_beds(str(title)),
            baths=p.get("bathrooms"),
            property_type=str(p.get("humanised-property-type") or ""),
            address=str(address),
            area=detect_area(str(address)),
            postcode=parse_postcode(str(address)),
            latitude=location.get("lat") if isinstance(location, dict) else None,
            longitude=location.get("lon") if isinstance(location, dict) else None,
            furnished=detect_furnished(text),
            bills_included=detect_bills_included(text),
            available_from=available,
            tenancy_start=available,
            description=strip_tags(" ".join(map(str, features))),
            image_url=image_url,
            raw=p,
        )

    def _parse_loose(self, html: str) -> list[RawListing]:
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
                            title=str(item.get("name") or "Property"),
                            price_pcm=price,
                            address=str(item.get("address") or ""),
                            area=detect_area(str(item.get("address") or "")),
                            raw=item,
                        )
                    )
        return out
