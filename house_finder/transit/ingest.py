"""Ingest BODS timetables into local route/stop tables, and query them.

The public entry points are:

* :func:`sync_transit` — refresh routes/stops from BODS + NaPTAN.
* :func:`available_routes` — routes available for the UI multi-select.
* :func:`filter_listings_by_routes` — apply the route/distance search filter.

Design notes
------------
BODS timetable datasets annotate stops with references but usually no
coordinates, so coordinates come from NaPTAN. A route is identified by
``line@operator`` (e.g. ``U1@First Bus``); stops are the union a line serves in
any pattern, restricted to the configured administrative areas so, for example,
a Bristol ``U1`` cannot bleed into Bath's ``U1``.
"""

from __future__ import annotations

import io
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import session_scope
from ..models import (
    TransitRoute,
    TransitRouteStop,
    TransitStop,
    utcnow,
)
from .bods import BodsClient, BodsError
from .naptan import area_code_of, fetch_area_stops
from .transxchange import (
    iter_zip_xml,
    line_names_in_file,
    normalise_line,
    parse_transxchange,
)

log = logging.getLogger(__name__)

MAX_DATASETS = 40


@dataclass
class RouteTarget:
    line: str
    line_norm: str
    operator: str | None = None

    def label(self) -> str:
        return self.line + (f"@{self.operator}" if self.operator else "")


@dataclass
class RouteAccum:
    line: str
    operator: str
    description: str = ""
    stops: dict[str, None] = field(default_factory=dict)
    datasets: list[int] = field(default_factory=list)

    def add(self, refs) -> None:
        for ref in refs:
            self.stops.setdefault(ref, None)


@dataclass
class TransitSyncResult:
    ok: bool = True
    datasets: int = 0
    routes: int = 0
    stops: int = 0
    errors: list[str] = field(default_factory=list)
    skipped_reason: str = ""
    duration_ms: int = 0

    def summary(self) -> str:
        if self.skipped_reason:
            return f"transit sync skipped: {self.skipped_reason}"
        text = (
            f"transit sync: {self.routes} routes, {self.stops} stops "
            f"from {self.datasets} dataset(s)"
        )
        if self.errors:
            text += f" ({len(self.errors)} warning(s))"
        return text


# --------------------------------------------------------------------------
# Parsing helpers
# --------------------------------------------------------------------------
def parse_route_targets(specs: list[str]) -> list[RouteTarget]:
    """Parse ``["U1", "20@Big Lemon"]`` into :class:`RouteTarget` objects."""
    targets: list[RouteTarget] = []
    for spec in specs:
        spec = (spec or "").strip()
        if not spec:
            continue
        if "@" in spec:
            line, operator = spec.split("@", 1)
            targets.append(
                RouteTarget(
                    line=line.strip(),
                    line_norm=normalise_line(line),
                    operator=operator.strip().lower() or None,
                )
            )
        else:
            targets.append(RouteTarget(line=spec, line_norm=normalise_line(spec)))
    return targets


def _dataset_matches(meta, targets: list[RouteTarget]) -> list[RouteTarget]:
    dataset_lines = {normalise_line(x) for x in meta.lines}
    operator = (meta.operator_name or "").lower()
    matched = []
    for target in targets:
        if target.line_norm not in dataset_lines:
            continue
        if target.operator and target.operator not in operator:
            continue
        matched.append(target)
    return matched


def _naptan_cache_dir() -> Path:
    settings = get_settings()
    if settings.bods_naptan_cache_dir:
        return Path(settings.bods_naptan_cache_dir)
    return Path(__file__).resolve().parent.parent.parent / "data" / "transit"


# --------------------------------------------------------------------------
# Sync
# --------------------------------------------------------------------------
def sync_transit(*, force: bool = False) -> TransitSyncResult:
    """Download the configured routes from BODS and persist their stops."""
    started = time.monotonic()
    settings = get_settings()
    if not settings.bods_api_key:
        return TransitSyncResult(
            ok=False,
            skipped_reason="HF_BODS_API_KEY is not configured",
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    targets = parse_route_targets(settings.bods_routes)
    if not targets:
        return TransitSyncResult(
            ok=False,
            skipped_reason="no bus routes configured (HF_BODS_ROUTES)",
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    areas = {a.strip() for a in settings.bods_admin_areas if a.strip()}
    client = BodsClient(settings.bods_api_key, timeout=settings.bods_timeout)
    errors: list[str] = []

    try:
        datasets = client.list_datasets(admin_areas=sorted(areas))
    except BodsError as exc:
        return TransitSyncResult(
            ok=False,
            errors=[str(exc)],
            skipped_reason="could not list BODS datasets",
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    accum: dict[str, RouteAccum] = {}
    stop_names: dict[str, str] = {}
    seen_datasets = 0

    for meta in datasets[:MAX_DATASETS]:
        matched = _dataset_matches(meta, targets)
        if not matched:
            continue
        wanted_lines = {t.line_norm for t in matched}
        try:
            payload = client.download_dataset(meta.id)
        except BodsError as exc:
            errors.append(f"dataset {meta.id}: {exc}")
            continue
        seen_datasets += 1
        try:
            members = list(iter_zip_xml(payload))
        except Exception:
            # Not a zip: treat the payload as a single XML document.
            members = [(meta.name or f"dataset-{meta.id}.xml", payload)]

        for _member_name, data in members:
            try:
                file_lines = line_names_in_file(data)
            except Exception:
                file_lines = set()
            if not (file_lines & wanted_lines):
                continue
            try:
                parsed = parse_transxchange(io.BytesIO(data), target_lines=wanted_lines)
            except Exception as exc:  # pragma: no cover - malformed member
                errors.append(f"dataset {meta.id} member: {exc!r}")
                continue

            for line_norm, line in parsed.lines.items():
                refs = [
                    ref
                    for ref in line.stops
                    if area_code_of(ref) in areas
                ]
                if not refs:
                    continue
                key = f"{line.line_name}@{meta.operator_name}"
                acc = accum.get(key)
                if acc is None:
                    acc = RouteAccum(
                        line=line.line_name,
                        operator=meta.operator_name or "",
                        description=meta.description or "",
                    )
                    accum[key] = acc
                acc.add(refs)
                if meta.id not in acc.datasets:
                    acc.datasets.append(meta.id)
                for ref in refs:
                    stop = parsed.stops.get(ref)
                    if stop and stop.name and ref not in stop_names:
                        stop_names[ref] = stop.name

    if not accum:
        return TransitSyncResult(
            ok=False,
            datasets=seen_datasets,
            errors=errors,
            skipped_reason="no matching routes found in BODS",
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    # Resolve stop coordinates from NaPTAN, area by area.
    used_refs = sorted({ref for acc in accum.values() for ref in acc.stops})
    needed_areas = sorted({area_code_of(ref) for ref in used_refs})
    cache_dir = _naptan_cache_dir()
    naptan: dict[str, object] = {}
    for area in needed_areas:
        try:
            naptan.update(
                fetch_area_stops(
                    area, cache_dir=cache_dir, timeout=settings.bods_timeout, force=force
                )
            )
        except Exception as exc:
            errors.append(f"NaPTAN area {area}: {exc}")

    with session_scope() as session:
        stops_written, routes_written = _persist(
            session, accum, stop_names, naptan
        )

    # Make sure listings have coordinates so the distance filter can use them.
    with session_scope() as session:
        try:
            from ..geocode import backfill_coordinates

            filled = backfill_coordinates(session)
            if filled:
                log.info("geocoded %d listing(s) for transit distances", filled)
        except Exception as exc:  # pragma: no cover - network/DB hiccup
            errors.append(f"geocode: {exc}")

    result = TransitSyncResult(
        ok=True,
        datasets=seen_datasets,
        routes=routes_written,
        stops=stops_written,
        errors=errors,
        duration_ms=int((time.monotonic() - started) * 1000),
    )
    log.info(result.summary())
    return result


def _persist(
    session: Session,
    accum: dict[str, RouteAccum],
    stop_names: dict[str, str],
    naptan: dict,
) -> tuple[int, int]:
    """Replace stored routes/stops, keeping the update atomic per transaction."""
    now = utcnow()

    # --- stops --------------------------------------------------------
    existing_stops = {
        s.atco_code: s for s in session.scalars(select(TransitStop)).all()
    }
    usable: dict[str, int] = {}
    for key, acc in accum.items():
        for ref in list(acc.stops):
            info = naptan.get(ref)
            if info is None:
                # No coordinates -> cannot measure distance; drop the stop.
                acc.stops.pop(ref, None)
                continue
            stop = existing_stops.get(ref)
            if stop is None:
                stop = TransitStop(atco_code=ref)
                session.add(stop)
                existing_stops[ref] = stop
            stop.name = info.name or stop_names.get(ref, "")
            stop.indicator = info.indicator
            stop.locality = info.locality
            stop.latitude = info.latitude
            stop.longitude = info.longitude
            stop.area_code = area_code_of(ref)
            usable[ref] = 1
    session.flush()

    # --- routes -------------------------------------------------------
    existing_routes = {
        r.key: r for r in session.scalars(select(TransitRoute)).all()
    }
    new_keys = set()
    for key, acc in accum.items():
        if not acc.stops:
            continue
        new_keys.add(key)
        route = existing_routes.get(key)
        if route is None:
            route = TransitRoute(key=key)
            session.add(route)
            existing_routes[key] = route
        route.line_name = acc.line
        route.operator = acc.operator
        route.description = acc.description
        route.stop_count = len(acc.stops)
        route.dataset_ids = ",".join(str(i) for i in acc.datasets)
        route.updated_at = now
        session.flush()

        session.execute(
            delete(TransitRouteStop).where(TransitRouteStop.route_id == route.id)
        )
        for seq, ref in enumerate(acc.stops):
            stop = existing_stops.get(ref)
            if stop is None or stop.id is None:
                continue
            session.add(
                TransitRouteStop(
                    route_id=route.id, stop_id=stop.id, sequence=seq
                )
            )
    session.flush()

    # Remove routes that are no longer produced.
    for key, route in existing_routes.items():
        if key not in new_keys:
            session.delete(route)
    session.flush()

    # Remove orphaned stops.
    referenced = set(
        session.scalars(select(TransitRouteStop.stop_id).distinct()).all()
    )
    for code, stop in existing_stops.items():
        if stop.id not in referenced:
            session.delete(stop)
    session.flush()

    return sum(usable.values()), len(new_keys)


# --------------------------------------------------------------------------
# Query helpers
# --------------------------------------------------------------------------
@dataclass
class StopPoint:
    route_key: str
    route_line: str
    stop_name: str
    latitude: float
    longitude: float


def available_routes() -> list[dict]:
    """Routes present in the database, ordered for display."""
    with session_scope() as session:
        rows = session.scalars(
            select(TransitRoute).order_by(
                TransitRoute.line_name, TransitRoute.operator
            )
        ).all()
        return [
            {
                "key": r.key,
                "line_name": r.line_name,
                "operator": r.operator,
                "stop_count": r.stop_count,
                "label": (
                    f"{r.line_name} · {r.operator}" if r.operator else r.line_name
                ),
                "updated_at": r.updated_at,
            }
            for r in rows
        ]


def _resolve_routes(session: Session, tokens: list[str]) -> tuple[list[TransitRoute], list[str]]:
    rows = session.scalars(select(TransitRoute)).all()
    matched: dict[int, TransitRoute] = {}
    unresolved: list[str] = []
    for token in tokens:
        token = (token or "").strip()
        if not token:
            continue
        low = token.lower()
        hits = [r for r in rows if r.key.lower() == low or r.line_name.lower() == low]
        if not hits:
            unresolved.append(token)
        for route in hits:
            matched[route.id] = route
    return list(matched.values()), unresolved


def route_points(session: Session, tokens: list[str]) -> tuple[list[StopPoint], list[str]]:
    """Return the stop points for the selected routes plus any unknown tokens."""
    routes, unresolved = _resolve_routes(session, tokens)
    if not routes:
        return [], unresolved
    by_id = {r.id: r for r in routes}
    stmt = (
        select(TransitRouteStop, TransitStop)
        .join(TransitStop, TransitRouteStop.stop_id == TransitStop.id)
        .where(TransitRouteStop.route_id.in_(by_id.keys()))
    )
    points = []
    for link, stop in session.execute(stmt).all():
        route = by_id[link.route_id]
        points.append(
            StopPoint(
                route_key=route.key,
                route_line=route.line_name,
                stop_name=stop.name,
                latitude=stop.latitude,
                longitude=stop.longitude,
            )
        )
    return points, unresolved


class _NearestIndex:
    """Precomputed point index for fast nearest-stop lookups."""

    def __init__(self, points: list[StopPoint]):
        import math

        self._points = points
        self._prep = [
            (p, math.radians(p.latitude), math.radians(p.longitude), math.cos(math.radians(p.latitude)))
            for p in points
        ]

    def nearest(self, lat: float, lon: float):
        import math

        lat_r = math.radians(lat)
        lon_r = math.radians(lon)
        cos_lat = math.cos(lat_r)
        best = None
        best_d = None
        for point, plat, plon, pcos in self._prep:
            dlat = lat_r - plat
            dlon = lon_r - plon
            a = (
                math.sin(dlat / 2) ** 2
                + cos_lat * pcos * math.sin(dlon / 2) ** 2
            )
            d = 2 * 6_371_008.8 * math.asin(min(1.0, math.sqrt(a)))
            if best_d is None or d < best_d:
                best, best_d = point, d
        return best, best_d


def filter_listings_by_routes(
    session: Session,
    listings: list,
    tokens: list[str],
    max_distance_m: int,
) -> tuple[list, dict[int, dict], str]:
    """Filter ``listings`` to those near a selected route.

    Returns ``(filtered, info_by_listing_id, warning)``. When no route data is
    available the listings are returned untouched (with a warning) rather than
    silently hiding everything.
    """
    tokens = [t for t in (tokens or []) if t and t.strip()]
    if not tokens:
        return list(listings), {}, ""

    points, unresolved = route_points(session, tokens)
    if not points:
        warning = "No stop data for the selected routes yet — run a transit sync."
        if unresolved:
            warning += f" Unknown: {', '.join(unresolved)}."
        return list(listings), {}, warning

    index = _NearestIndex(points)
    filtered: list = []
    info: dict[int, dict] = {}
    for listing in listings:
        if listing.latitude is None or listing.longitude is None:
            continue
        point, distance = index.nearest(listing.latitude, listing.longitude)
        if point is None:
            continue
        if max_distance_m and distance > max_distance_m:
            continue
        filtered.append(listing)
        info[listing.id] = {
            "route_key": point.route_key,
            "route": point.route_line,
            "stop": point.stop_name,
            "distance_m": int(round(distance)),
        }
    warning = ""
    if unresolved:
        warning = "Unknown route(s): " + ", ".join(unresolved)
    return filtered, info, warning
