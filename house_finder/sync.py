"""Sync service: run all enabled sources for a query and persist results."""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

from sqlalchemy import select

from .config import get_settings
from .db import session_scope
from .models import Listing, ListingStatus, SyncRun, SyncSourceResult, SyncStatus
from .pipeline import dedupe_listings, ingest
from .schemas import SearchQuery
from .sources import SourceBlocked, SourceError, get_sources

log = logging.getLogger(__name__)


def run_sync(
    query: SearchQuery | None = None,
    *,
    source_names: list[str] | None = None,
    notify: bool = True,
) -> SyncRun:
    """Run a full sync across sources. Returns the persisted SyncRun."""
    settings = get_settings()
    query = query or SearchQuery(
        min_beds=settings.default_beds,
        location=settings.default_location,
        max_price=settings.default_max_price,
        min_price=settings.default_min_price,
        max_results=settings.max_results_per_source,
    )
    sources = get_sources(source_names)

    with session_scope() as session:
        run = SyncRun(
            started_at=datetime.now(timezone.utc),
            status=SyncStatus.RUNNING,
            sources=",".join(s.name for s in sources),
        )
        session.add(run)
        session.flush()
        run_id = run.id

    all_new: list[Listing] = []
    errors: list[str] = []
    blocked: list[str] = []
    total_found = total_new = total_updated = 0
    ok_count = 0

    for source in sources:
        started = time.monotonic()
        try:
            raws = source.fetch(query)
            with session_scope() as session:
                stats = ingest(session, raws)
                session.add(
                    SyncSourceResult(
                        run_id=run_id,
                        source=source.name,
                        found=stats.found,
                        new=stats.new,
                        ok=True,
                        duration_ms=int((time.monotonic() - started) * 1000),
                    )
                )
                all_new.extend(stats.new_listings)
            total_found += stats.found
            total_new += stats.new
            total_updated += stats.updated
            ok_count += 1
            log.info(
                "sync %s: found=%d new=%d updated=%d",
                source.name,
                stats.found,
                stats.new,
                stats.updated,
            )
        except SourceBlocked as exc:
            # A known anti-bot block (e.g. OpenRent from a datacenter IP) is a
            # benign skip, not a failure: record it but keep the run successful.
            blocked.append(f"{source.name}: {exc}")
            log.warning("sync %s blocked: %s", source.name, exc)
            with session_scope() as session:
                session.add(
                    SyncSourceResult(
                        run_id=run_id,
                        source=source.name,
                        ok=True,
                        error=f"blocked: {exc}"[:2000],
                        duration_ms=int((time.monotonic() - started) * 1000),
                    )
                )
            ok_count += 1
        except SourceError as exc:
            errors.append(f"{source.name}: {exc}")
            log.warning("sync %s failed: %s", source.name, exc)
            with session_scope() as session:
                session.add(
                    SyncSourceResult(
                        run_id=run_id,
                        source=source.name,
                        ok=False,
                        error=str(exc)[:2000],
                        duration_ms=int((time.monotonic() - started) * 1000),
                    )
                )
        except Exception as exc:  # pragma: no cover - unexpected
            errors.append(f"{source.name}: {exc!r}")
            log.exception("sync %s crashed", source.name)

    # Collapse the same property listed on multiple sites onto one row.
    with session_scope() as session:
        dupes = dedupe_listings(session)
    if dupes:
        log.info("dedupe: %d duplicate listing(s) linked", dupes)

    status = SyncStatus.SUCCESS
    if errors and ok_count == 0:
        status = SyncStatus.FAILED
    elif errors:
        status = SyncStatus.PARTIAL

    with session_scope() as session:
        run = session.get(SyncRun, run_id)
        run.finished_at = datetime.now(timezone.utc)
        run.status = status
        run.found = total_found
        run.new = total_new
        run.updated = total_updated
        run.errors = "\n".join(errors)

    if notify and total_new and settings.notify_enabled and settings.notify_on_new:
        try:
            from .notify import notify_new_listings

            notify_new_listings(all_new)
        except Exception:  # pragma: no cover
            log.exception("notification failed")

    with session_scope() as session:
        return session.get(SyncRun, run_id)


def mark_seen(listing_ids: list[int] | None = None) -> int:
    """Mark NEW listings as SEEN. If ids is None, mark all NEW."""
    with session_scope() as session:
        stmt = select(Listing).where(Listing.status == ListingStatus.NEW)
        if listing_ids:
            stmt = stmt.where(Listing.id.in_(listing_ids))
        rows = session.scalars(stmt).all()
        for row in rows:
            row.status = ListingStatus.SEEN
        return len(rows)
