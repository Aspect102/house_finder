"""Postcode geocoding via postcodes.io, with a DB-backed cache.

Rightmove/OnTheMarket/UniHomes supply coordinates directly; OpenRent and Zoopla
usually only supply a postcode. Since bus-stop proximity needs coordinates, we
fill the gap from postcodes.io (full postcode first, outcode as a fallback) and
cache the result so repeated syncs make no extra requests. Geocoding is always
best-effort: any failure simply leaves the listing un-geocoded.
"""

from __future__ import annotations

import logging
from urllib.parse import quote

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import get_settings
from .models import GeocodeCache, Listing
from .normalize import normalise_postcode_key

log = logging.getLogger(__name__)

BASE_URL = "https://api.postcodes.io"
_HEADERS = {"User-Agent": "house-finder/0.3 (personal use)"}


def _is_full_postcode(postcode: str) -> bool:
    key = normalise_postcode_key(postcode)
    # Full UK postcodes are 5-7 chars (e.g. BA23AB); outcodes are 2-4.
    return len(key) > 4 and key[-1].isalpha() and any(c.isdigit() for c in key)


def lookup_postcode(
    postcode: str, *, timeout: int | None = None
) -> tuple[float | None, float | None]:
    """Resolve a postcode/outcode to ``(lat, lon)`` via postcodes.io."""
    postcode = (postcode or "").strip()
    if not postcode:
        return None, None
    settings = get_settings()
    timeout = timeout or settings.geocode_timeout
    if _is_full_postcode(postcode):
        path = f"/postcodes/{quote(normalise_postcode_key(postcode))}"
        outcode_path = f"/outcodes/{quote(normalise_postcode_key(postcode)[:-3])}"
    else:
        path = None
        outcode_path = f"/outcodes/{quote(normalise_postcode_key(postcode))}"

    for candidate in (path, outcode_path):
        if not candidate:
            continue
        try:
            resp = httpx.get(
                BASE_URL + candidate, timeout=timeout, headers=_HEADERS
            )
            if resp.status_code != 200:
                continue
            result = resp.json().get("result") or {}
            lat = result.get("latitude")
            lon = result.get("longitude")
            if lat is not None and lon is not None:
                return float(lat), float(lon)
        except (httpx.HTTPError, ValueError):
            continue
    return None, None


def resolve_coords(
    session: Session,
    postcode: str,
    latitude: float | None,
    longitude: float | None,
    *,
    cache: dict[str, tuple[float | None, float | None]] | None = None,
) -> tuple[float | None, float | None]:
    """Return coordinates for a listing, using the cache/DB when possible."""
    if latitude is not None and longitude is not None:
        return latitude, longitude
    if not postcode or not get_settings().geocode_enabled:
        return latitude, longitude

    key = normalise_postcode_key(postcode)
    if cache is not None and key in cache:
        return cache[key]

    row = session.scalar(select(GeocodeCache).where(GeocodeCache.key == key))
    if row is not None:
        result = (row.latitude, row.longitude)
    else:
        lat, lon = lookup_postcode(postcode)
        result = (lat, lon)
        if lat is not None and lon is not None:
            session.add(
                GeocodeCache(
                    key=key, postcode=postcode, latitude=lat, longitude=lon
                )
            )
            session.flush()

    if cache is not None:
        cache[key] = result
    return result


def backfill_coordinates(session: Session, *, limit: int | None = None) -> int:
    """Fill coordinates for listings that have a postcode but no lat/lon.

    Returns the number of listings updated. Uses the same cache as the ingest
    pipeline, so this is cheap to run repeatedly.
    """
    stmt = select(Listing).where(
        Listing.latitude.is_(None),
        Listing.postcode != "",
    )
    if limit:
        stmt = stmt.limit(limit)
    rows = session.scalars(stmt).all()
    cache: dict[str, tuple[float | None, float | None]] = {}
    updated = 0
    for listing in rows:
        lat, lon = resolve_coords(
            session, listing.postcode, None, None, cache=cache
        )
        if lat is not None and lon is not None:
            listing.latitude = lat
            listing.longitude = lon
            updated += 1
    session.flush()
    return updated
