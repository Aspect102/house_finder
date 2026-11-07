"""Ingestion pipeline: normalise raw listings, dedupe, persist, track state."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Listing, ListingStatus
from .geocode import resolve_coords
from .normalize import (
    content_hash,
    dedupe_fingerprint,
    detect_area,
    detect_bills_included,
    detect_furnished,
    is_available_now,
    parse_beds,
    parse_date,
    parse_postcode,
    price_per_person_per_week,
    whole_property_price,
)
from .schemas import RawListing

log = logging.getLogger(__name__)


@dataclass
class IngestStats:
    found: int = 0
    new: int = 0
    updated: int = 0
    price_changes: int = 0
    new_listings: list[Listing] = field(default_factory=list)


def _clean(raw: RawListing) -> dict:
    """Fill in derived fields from whatever the adapter provided."""
    text = " ".join(
        filter(None, [raw.title, raw.address, raw.description, raw.property_type])
    )
    beds = raw.beds if raw.beds is not None else parse_beds(raw.title)
    area = raw.area or detect_area(f"{raw.address} {raw.title}")
    postcode = raw.postcode or parse_postcode(raw.address)
    furnished = raw.furnished if raw.furnished is not None else detect_furnished(text)
    bills = (
        raw.bills_included
        if raw.bills_included is not None
        else detect_bills_included(text)
    )
    # Tenancy start: prefer an explicit ISO date from the adapter, else parse
    # the advertised "available from" text. "Now"/"ASAP" means available now.
    tenancy_start = parse_date(raw.tenancy_start) or parse_date(raw.available_from)
    available_now = (
        raw.available_now
        if raw.available_now is not None
        else is_available_now(raw.available_from)
    )
    # Normalise per-person student rents to a whole-property monthly figure.
    price_pcm = whole_property_price(raw.price_pcm, beds)
    address = raw.address.strip()
    return {
        "title": raw.title.strip(),
        "price_pcm": price_pcm,
        "price_pppw": price_per_person_per_week(price_pcm, beds),
        "beds": beds,
        "baths": raw.baths,
        "property_type": raw.property_type.strip(),
        "address": address,
        "area": area,
        "postcode": postcode,
        "latitude": raw.latitude,
        "longitude": raw.longitude,
        "furnished": furnished,
        "bills_included": bills,
        "available_from": raw.available_from.strip(),
        "tenancy_start": tenancy_start,
        "available_now": bool(available_now),
        "description": raw.description.strip(),
        "image_url": raw.image_url.strip(),
        "dedupe_key": dedupe_fingerprint(address, postcode, beds, price_pcm),
    }


def ingest(session: Session, raws: list[RawListing]) -> IngestStats:
    """Upsert a batch of raw listings, tracking new/updated/price changes."""
    stats = IngestStats()
    now = datetime.now(timezone.utc)
    seen_keys: set[tuple[str, str]] = set()
    geo_cache: dict[str, tuple[float | None, float | None]] = {}

    for raw in raws:
        if not raw.source_id:
            continue
        key = (raw.source, raw.source_id)
        # Sources can return the same listing twice in one page; keep the first.
        if key in seen_keys:
            continue
        seen_keys.add(key)
        stats.found += 1
        fields = _clean(raw)

        existing = session.scalar(
            select(Listing).where(
                Listing.source == raw.source, Listing.source_id == raw.source_id
            )
        )

        # Fill missing coordinates from the postcode (postcodes.io) so bus-stop
        # proximity works even for sources that omit coordinates.
        lat, lon = resolve_coords(
            session,
            fields["postcode"],
            fields["latitude"],
            fields["longitude"],
            cache=geo_cache,
        )
        if lat is None and existing is not None and existing.latitude is not None:
            # Don't wipe coordinates we resolved on an earlier sync.
            lat, lon = existing.latitude, existing.longitude
        fields["latitude"] = lat
        fields["longitude"] = lon

        chash = content_hash(
            fields["title"],
            fields["price_pcm"],
            fields["beds"],
            fields["address"],
            fields["description"],
            fields["tenancy_start"],
        )

        if existing is None:
            listing = Listing(
                source=raw.source,
                source_id=raw.source_id,
                url=raw.url,
                status=ListingStatus.NEW,
                is_active=True,
                content_hash=chash,
                first_seen=now,
                last_seen=now,
                raw_json=json.dumps(raw.raw, default=str)[:20000],
                **fields,
            )
            session.add(listing)
            stats.new += 1
            stats.new_listings.append(listing)
            continue

        # Existing listing — detect changes.
        changed = existing.content_hash != chash
        if existing.price_pcm != fields["price_pcm"]:
            existing.previous_price_pcm = existing.price_pcm
            existing.price_changed_at = now
            stats.price_changes += 1
        for key, value in fields.items():
            setattr(existing, key, value)
        existing.last_seen = now
        existing.is_active = True
        existing.content_hash = chash
        if changed:
            stats.updated += 1

    session.flush()
    return stats


#: Preference order when choosing the canonical row of a duplicate group.
SOURCE_PRIORITY = ("rightmove", "openrent", "unihomes", "onthemarket", "zoopla", "spareroom")


def _source_rank(source: str) -> int:
    try:
        return SOURCE_PRIORITY.index(source)
    except ValueError:
        return len(SOURCE_PRIORITY)


def dedupe_listings(session: Session) -> int:
    """Link cross-source duplicates to one canonical listing.

    Recomputes ``duplicate_of_id`` for every listing from its ``dedupe_key``.
    Within a group the canonical row is the highest-priority source, then the
    active, geo-located, priced, earliest one; every other row is marked as a
    duplicate and hidden from search results. Returns the number linked.
    """
    listings = session.scalars(select(Listing)).all()
    for listing in listings:
        listing.duplicate_of_id = None
        # Recompute so existing rows are backfilled and edits are picked up.
        listing.dedupe_key = dedupe_fingerprint(
            listing.address, listing.postcode, listing.beds, listing.price_pcm
        )

    groups: dict[str, list[Listing]] = {}
    for listing in listings:
        if listing.dedupe_key:
            groups.setdefault(listing.dedupe_key, []).append(listing)

    now = datetime.now(timezone.utc)
    marked = 0
    for group in groups.values():
        if len(group) < 2:
            continue
        group.sort(
            key=lambda l: (
                _source_rank(l.source),
                0 if l.is_active else 1,
                0 if l.latitude is not None else 1,
                0 if l.price_pcm else 1,
                l.first_seen or now,
                l.id or 0,
            )
        )
        canonical = group[0]
        for duplicate in group[1:]:
            if duplicate.id == canonical.id:
                continue
            duplicate.duplicate_of_id = canonical.id
            marked += 1
    session.flush()
    return marked


def duplicate_sources(session: Session, listing_ids: list[int]) -> dict[int, list[str]]:
    """Map canonical listing id -> sources of its hidden duplicates."""
    if not listing_ids:
        return {}
    rows = session.execute(
        select(Listing.duplicate_of_id, Listing.source).where(
            Listing.duplicate_of_id.in_(listing_ids)
        )
    ).all()
    out: dict[int, list[str]] = {}
    for canonical_id, source in rows:
        sources = out.setdefault(canonical_id, [])
        if source not in sources:
            sources.append(source)
    return out
