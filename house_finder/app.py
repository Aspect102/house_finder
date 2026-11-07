"""FastAPI application: REST API + HTMX web UI."""

from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import __version__
from .config import get_settings
from .db import get_db, init_db
from .models import Listing, ListingStatus, SearchProfile, SyncRun
from .profiles import (
    params_from_query,
    profile_apply_qs,
    profile_sources,
    query_from_params,
    run_profile,
)
from .schemas import (
    ListingOut,
    NearbyRoute,
    ProfileIn,
    ProfileOut,
    SearchQuery,
    SyncRunOut,
)
from .scheduler import get_scheduler, shutdown_scheduler, start_scheduler
from .sources import available_sources
from .sync import mark_seen, run_sync
from .transit import (
    annotate_listings_by_routes,
    available_routes as transit_available_routes,
    filter_listings_by_routes,
    sync_transit,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    start_scheduler()
    yield
    shutdown_scheduler()


app = FastAPI(title="House Finder", version=__version__, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _apply_filters(stmt, *, status, source, min_beds, max_beds, min_price, max_price,
                   area, furnished, bills, q, active_only,
                   tenancy_start_from="", tenancy_start_to="", available_now=None):
    if status:
        stmt = stmt.where(Listing.status == ListingStatus(status))
    if source:
        stmt = stmt.where(Listing.source == source)
    if min_beds:
        stmt = stmt.where(Listing.beds >= min_beds)
    if max_beds:
        stmt = stmt.where(Listing.beds <= max_beds)
    if min_price:
        stmt = stmt.where(Listing.price_pcm >= min_price)
    if max_price:
        stmt = stmt.where(Listing.price_pcm <= max_price)
    if area:
        stmt = stmt.where(Listing.area == area)
    if furnished is not None:
        stmt = stmt.where(Listing.furnished == furnished)
    if bills is not None:
        stmt = stmt.where(Listing.bills_included == bills)
    # Tenancy start window. Listings with no parsed date are excluded when a
    # window is requested (they cannot be verified against it).
    start = _to_date(tenancy_start_from)
    end = _to_date(tenancy_start_to)
    if start:
        stmt = stmt.where(Listing.tenancy_start >= start)
    if end:
        stmt = stmt.where(Listing.tenancy_start <= end)
    if available_now is True:
        stmt = stmt.where(Listing.available_now.is_(True))
    elif available_now is False:
        stmt = stmt.where(Listing.available_now.is_(False))
    if q:
        like = f"%{q}%"
        stmt = stmt.where(
            Listing.title.ilike(like)
            | Listing.address.ilike(like)
            | Listing.description.ilike(like)
        )
    if active_only:
        stmt = stmt.where(Listing.is_active.is_(True))
    return stmt


def _order(stmt, sort: str):
    mapping = {
        "price_asc": Listing.price_pcm.asc().nulls_last(),
        "price_desc": Listing.price_pcm.desc().nulls_last(),
        "beds_desc": Listing.beds.desc().nulls_last(),
        "beds_asc": Listing.beds.asc().nulls_last(),
        "newest": Listing.first_seen.desc(),
        "pppw_asc": Listing.price_pppw.asc().nulls_last(),
        "tenancy_asc": Listing.tenancy_start.asc().nulls_last(),
        "tenancy_desc": Listing.tenancy_start.desc().nulls_last(),
    }
    return stmt.order_by(mapping.get(sort, Listing.first_seen.desc()))


def _to_int(value: str | int | None, default: int = 0) -> int:
    """Parse an optional numeric query/form value; empty strings become default."""
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _to_bool(value: str | bool | None, default: bool = True) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _to_date(value: str | None):
    """Parse an optional ISO/free-text date; empty or invalid becomes None."""
    if not value:
        return None
    from .normalize import parse_date

    return parse_date(value)


def _to_tri_bool(value: str | bool | None) -> bool | None:
    """Map yes/no/any to True/False/None."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    low = value.strip().lower()
    if low in {"yes", "true", "1", "on"}:
        return True
    if low in {"no", "false", "0", "off"}:
        return False
    return None


def _parse_routes(value: str | list[str] | None) -> list[str]:
    """Parse a route selection into a list of tokens.

    Accepts either a comma-separated string or a list (repeated query params or
    a multi-select), and flattens commas in either case.
    """
    if value is None or value == "":
        return []
    if isinstance(value, (list, tuple)):
        items = value
    else:
        items = [value]
    tokens: list[str] = []
    for item in items:
        tokens.extend(str(item).split(","))
    return [t.strip() for t in tokens if t.strip()]


def _transit_context(
    db: Session,
    listings: list,
    routes: list[str],
    max_distance_m: int,
    mode: str = "filter",
) -> tuple[list, dict, str]:
    """Apply the bus-route filter/labelling and return (listings, info, warning)."""
    if mode == "annotate":
        return annotate_listings_by_routes(
            db, listings, max_distance_m, all_routes=True
        )
    return filter_listings_by_routes(db, listings, routes, max_distance_m)


def _listing_out(listing: Listing, info: list[dict] | None = None) -> ListingOut:
    out = ListingOut.model_validate(listing)
    if info:
        best = info[0]
        out = out.model_copy(
            update={
                "nearest_route": best.get("route"),
                "nearest_stop": best.get("stop"),
                "nearest_stop_m": best.get("distance_m"),
                "nearby_routes": [NearbyRoute(**m) for m in info],
            }
        )
    return out


def _stats_context(db: Session) -> dict:
    """Shared context for the stats strip (used by index and sync responses)."""
    rows = db.execute(
        select(Listing.status, func.count()).group_by(Listing.status)
    ).all()
    # SQLAlchemy stores Enum members; templates look up plain string keys.
    counts = {
        (key.value if hasattr(key, "value") else str(key)): value
        for key, value in rows
    }
    last_run = db.scalars(
        select(SyncRun).order_by(SyncRun.started_at.desc()).limit(1)
    ).first()
    active_total = db.scalar(
        select(func.count()).select_from(Listing).where(Listing.is_active.is_(True))
    ) or 0
    return {
        "counts": counts,
        "total": sum(counts.values()),
        "active_total": active_total,
        "last_run": last_run,
        "scheduler": get_scheduler(),
        "settings": get_settings(),
    }


def _profiles_context(db: Session) -> dict:
    profiles = db.scalars(
        select(SearchProfile).order_by(SearchProfile.name)
    ).all()
    return {
        "profiles": [
            {
                "id": p.id,
                "name": p.name,
                "enabled": p.enabled,
                "apply_qs": profile_apply_qs(p),
            }
            for p in profiles
        ]
    }


def _listings_for_query(
    db: Session, query: SearchQuery, *, sort: str = "newest", limit: int = 500
) -> tuple[list[Listing], dict, str]:
    stmt = _apply_filters(
        select(Listing),
        status="",
        source="",
        min_beds=query.min_beds,
        max_beds=query.max_beds or 0,
        min_price=query.min_price,
        max_price=query.max_price,
        area="",
        furnished=query.furnished,
        bills=query.bills_included,
        q=query.keywords,
        active_only=True,
        tenancy_start_from=query.tenancy_start_from,
        tenancy_start_to=query.tenancy_start_to,
        available_now=query.available_now,
    )
    listings = db.scalars(_order(stmt, sort).limit(limit)).all()
    return _transit_context(
        db,
        listings,
        query.routes,
        query.max_stop_distance_m,
        query.route_mode,
    )


# --------------------------------------------------------------------------
# Web UI
# --------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def index(
    request: Request,
    db: Session = Depends(get_db),
    status: str = Query(""),
    source: str = Query(""),
    min_beds: str | None = Query(None),
    max_beds: str = Query(""),
    min_price: str = Query(""),
    max_price: str = Query(""),
    area: str = Query(""),
    furnished: str = Query(""),
    bills: str = Query(""),
    tenancy_start_from: str = Query(""),
    tenancy_start_to: str = Query(""),
    available_now: str = Query(""),
    q: str = Query(""),
    sort: str = Query("newest"),
    active_only: str = Query("true"),
    routes: list[str] = Query(default=[]),
    max_stop_distance_m: str | None = Query(None),
    route_mode: str = Query("filter"),
):
    settings = get_settings()
    furnished_val = {"yes": True, "no": False}.get(furnished)
    bills_val = {"yes": True, "no": False}.get(bills)
    # First load with no parameter uses the configured default (6). An
    # explicitly cleared field arrives as "" and means "no minimum".
    if min_beds is None:
        min_beds = str(settings.default_beds)
    # Same convention for the bus-stop proximity: absent -> configured default,
    # explicitly cleared -> unlimited (0).
    if max_stop_distance_m is None:
        max_stop_distance_m = str(settings.default_max_stop_distance_m)
    min_beds_i = _to_int(min_beds)
    max_beds_i = _to_int(max_beds)
    min_price_i = _to_int(min_price)
    max_price_i = _to_int(max_price)
    active_only_b = _to_bool(active_only)
    routes_list = _parse_routes(routes)
    max_distance_i = _to_int(max_stop_distance_m, settings.default_max_stop_distance_m)
    mode = route_mode if route_mode in {"filter", "annotate"} else "filter"

    stmt = select(Listing)
    stmt = _apply_filters(
        stmt,
        status=status,
        source=source,
        min_beds=min_beds_i,
        max_beds=max_beds_i,
        min_price=min_price_i,
        max_price=max_price_i,
        area=area,
        furnished=furnished_val,
        bills=bills_val,
        q=q,
        active_only=active_only_b,
        tenancy_start_from=tenancy_start_from,
        tenancy_start_to=tenancy_start_to,
        available_now=_to_tri_bool(available_now),
    )
    stmt = _order(stmt, sort)
    listings = db.scalars(stmt.limit(500)).all()
    listings, transit_info, transit_warning = _transit_context(
        db, list(listings), routes_list, max_distance_i, mode
    )

    areas = [
        r[0]
        for r in db.execute(
            select(Listing.area).where(Listing.area != "").distinct().order_by(Listing.area)
        ).all()
    ]

    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "listings": listings,
            "areas": areas,
            "sources": available_sources(),
            "transit_routes": transit_available_routes(),
            "transit_info": transit_info,
            "transit_warning": transit_warning,
            **_stats_context(db),
            **_profiles_context(db),
            "filters": {
                "status": status,
                "source": source,
                "min_beds": min_beds,
                "max_beds": max_beds,
                "min_price": min_price,
                "max_price": max_price,
                "area": area,
                "furnished": furnished,
                "bills": bills,
                "tenancy_start_from": tenancy_start_from,
                "tenancy_start_to": tenancy_start_to,
                "available_now": available_now,
                "q": q,
                "sort": sort,
                "active_only": active_only_b,
                "routes": ",".join(routes_list),
                "routes_list": routes_list,
                "max_stop_distance_m": max_stop_distance_m,
                "route_mode": mode,
            },
        },
    )


@app.get("/partials/listings", response_class=HTMLResponse)
def listings_partial(
    request: Request,
    db: Session = Depends(get_db),
    status: str = Query(""),
    source: str = Query(""),
    min_beds: str | None = Query(None),
    max_beds: str = Query(""),
    min_price: str = Query(""),
    max_price: str = Query(""),
    area: str = Query(""),
    furnished: str = Query(""),
    bills: str = Query(""),
    tenancy_start_from: str = Query(""),
    tenancy_start_to: str = Query(""),
    available_now: str = Query(""),
    q: str = Query(""),
    sort: str = Query("newest"),
    active_only: str = Query("true"),
    routes: list[str] = Query(default=[]),
    max_stop_distance_m: str | None = Query(None),
    route_mode: str = Query("filter"),
):
    furnished_val = {"yes": True, "no": False}.get(furnished)
    bills_val = {"yes": True, "no": False}.get(bills)
    settings = get_settings()
    if min_beds is None:
        min_beds = str(settings.default_beds)
    if max_stop_distance_m is None:
        max_stop_distance_m = str(settings.default_max_stop_distance_m)
    mode = route_mode if route_mode in {"filter", "annotate"} else "filter"
    stmt = _apply_filters(
        select(Listing),
        status=status,
        source=source,
        min_beds=_to_int(min_beds),
        max_beds=_to_int(max_beds),
        min_price=_to_int(min_price),
        max_price=_to_int(max_price),
        area=area,
        furnished=furnished_val,
        bills=bills_val,
        q=q,
        active_only=_to_bool(active_only),
        tenancy_start_from=tenancy_start_from,
        tenancy_start_to=tenancy_start_to,
        available_now=_to_tri_bool(available_now),
    )
    listings = db.scalars(_order(stmt, sort).limit(500)).all()
    listings, transit_info, transit_warning = _transit_context(
        db,
        list(listings),
        _parse_routes(routes),
        _to_int(max_stop_distance_m, settings.default_max_stop_distance_m),
        mode,
    )
    return templates.TemplateResponse(
        request,
        "partials/listing_cards.html",
        {
            "listings": listings,
            "transit_info": transit_info,
            "transit_warning": transit_warning,
        },
    )


@app.post("/listings/{listing_id}/status")
def set_status(
    request: Request,
    listing_id: int,
    status: str = Form(...),
    db: Session = Depends(get_db),
):
    listing = db.get(Listing, listing_id)
    if not listing:
        raise HTTPException(404, "Listing not found")
    try:
        listing.status = ListingStatus(status)
    except ValueError as exc:
        raise HTTPException(400, f"Invalid status '{status}'") from exc
    db.commit()
    db.refresh(listing)
    # HTMX requests get just the updated card back; plain form posts redirect.
    if request.headers.get("HX-Request") == "true":
        return templates.TemplateResponse(
            request,
            "partials/listing_card_response.html",
            {"l": listing, **_stats_context(db)},
        )
    return RedirectResponse("/", status_code=303)


@app.post("/listings/mark-seen")
def mark_all_seen(db: Session = Depends(get_db)):
    mark_seen()
    return RedirectResponse("/", status_code=303)


@app.post("/sync")
def trigger_sync(
    request: Request,
    db: Session = Depends(get_db),
    min_beds: str = Form(""),
    max_price: str = Form(""),
    source: str = Form(""),
    tenancy_start_from: str = Form(""),
    tenancy_start_to: str = Form(""),
):
    settings = get_settings()
    min_beds_i = _to_int(min_beds, settings.default_beds)
    max_price_i = _to_int(max_price, settings.default_max_price)
    query = SearchQuery(
        min_beds=min_beds_i,
        max_price=max_price_i,
        tenancy_start_from=tenancy_start_from,
        tenancy_start_to=tenancy_start_to,
        max_results=settings.max_results_per_source,
    )
    names = [source] if source else None
    run_sync(query, source_names=names)
    if request.headers.get("HX-Request") == "true":
        listings = db.scalars(
            _order(
                _apply_filters(
                    select(Listing),
                    status="",
                    source="",
                    min_beds=min_beds_i,
                    max_beds=0,
                    min_price=0,
                    max_price=max_price_i,
                    area="",
                    furnished=None,
                    bills=None,
                    q="",
                    active_only=True,
                    tenancy_start_from=tenancy_start_from,
                    tenancy_start_to=tenancy_start_to,
                ),
                "newest",
            ).limit(500)
        ).all()
        return templates.TemplateResponse(
            request,
            "partials/sync_result.html",
            {"listings": listings, "transit_info": {}, **_stats_context(db)},
        )
    return RedirectResponse("/", status_code=303)


# --------------------------------------------------------------------------
# Saved searches (profiles)
# --------------------------------------------------------------------------
@app.post("/profiles")
def save_profile(
    db: Session = Depends(get_db),
    name: str = Form(...),
    min_beds: str = Form(""),
    max_beds: str = Form(""),
    min_price: str = Form(""),
    max_price: str = Form(""),
    tenancy_start_from: str = Form(""),
    tenancy_start_to: str = Form(""),
    q: str = Form(""),
    furnished: str = Form(""),
    bills: str = Form(""),
    source: str = Form(""),
    available_now: str = Form(""),
    routes: str = Form(""),
    max_stop_distance_m: str = Form(""),
    route_mode: str = Form("filter"),
):
    """Save the current UI filters as a reusable, schedulable search."""
    settings = get_settings()
    query = SearchQuery(
        location=settings.default_location,
        min_beds=_to_int(min_beds, settings.default_beds),
        max_beds=_to_int(max_beds) or None,
        min_price=_to_int(min_price),
        max_price=_to_int(max_price),
        tenancy_start_from=tenancy_start_from,
        tenancy_start_to=tenancy_start_to,
        keywords=q,
        furnished={"yes": True, "no": False}.get(furnished),
        bills_included={"yes": True, "no": False}.get(bills),
        available_now=_to_tri_bool(available_now),
        routes=_parse_routes(routes),
        max_stop_distance_m=_to_int(
            max_stop_distance_m, settings.default_max_stop_distance_m
        ),
        route_mode=route_mode,
        max_results=settings.max_results_per_source,
    )
    params = params_from_query(
        query, sources=[source] if source else None
    )
    safe_name = name.strip()[:120]
    if not safe_name:
        raise HTTPException(400, "Profile name is required")
    existing = db.scalar(select(SearchProfile).where(SearchProfile.name == safe_name))
    if existing is None:
        db.add(
            SearchProfile(
                name=safe_name,
                params_json=json.dumps(params),
                enabled=True,
            )
        )
    else:
        existing.params_json = json.dumps(params)
        existing.enabled = True
    db.commit()
    return RedirectResponse("/", status_code=303)


@app.post("/profiles/{profile_id}/delete")
def delete_profile(profile_id: int, db: Session = Depends(get_db)):
    profile = db.get(SearchProfile, profile_id)
    if profile is None:
        raise HTTPException(404, "Profile not found")
    db.delete(profile)
    db.commit()
    return RedirectResponse("/", status_code=303)


@app.post("/profiles/{profile_id}/run")
def run_saved_profile(
    request: Request,
    profile_id: int,
    db: Session = Depends(get_db),
):
    profile = db.get(SearchProfile, profile_id)
    if profile is None:
        raise HTTPException(404, "Profile not found")
    params = json.loads(profile.params_json or "{}")
    query = query_from_params(params)
    try:
        run_profile(profile_id)
    except KeyError as exc:  # pragma: no cover - race with delete
        raise HTTPException(404, str(exc)) from exc
    db.expire_all()
    if request.headers.get("HX-Request") == "true":
        listings, transit_info, transit_warning = _listings_for_query(db, query)
        return templates.TemplateResponse(
            request,
            "partials/sync_result.html",
            {
                "listings": listings,
                "transit_info": transit_info,
                "transit_warning": transit_warning,
                **_stats_context(db),
            },
        )
    return RedirectResponse("/", status_code=303)


# --------------------------------------------------------------------------
# REST API
# --------------------------------------------------------------------------
@app.get("/api/health")
def health():
    return {"status": "ok", "version": __version__}


@app.get("/api/sources")
def api_sources():
    return {"sources": available_sources(), "enabled": get_settings().sources}


# --------------------------------------------------------------------------
# Transit (bus routes)
# --------------------------------------------------------------------------
@app.get("/api/transit/routes")
def api_transit_routes():
    routes = transit_available_routes()
    return {"routes": routes, "count": len(routes)}


@app.post("/api/transit/sync")
def api_transit_sync(force: bool = Query(False)):
    result = sync_transit(force=force)
    return {
        "ok": result.ok,
        "datasets": result.datasets,
        "routes": result.routes,
        "stops": result.stops,
        "errors": result.errors,
        "skipped_reason": result.skipped_reason,
        "duration_ms": result.duration_ms,
    }


@app.post("/transit/sync")
def ui_transit_sync(request: Request, force: str = Form("")):
    result = sync_transit(force=_to_bool(force, False))
    if request.headers.get("HX-Request") == "true":
        css = "hint" if result.ok else "hint warn"
        return HTMLResponse(f'<span id="transit-status" class="{css}">{result.summary()}</span>')
    return RedirectResponse("/", status_code=303)


@app.get("/api/listings", response_model=list[ListingOut])
def api_listings(
    db: Session = Depends(get_db),
    status: str = Query(""),
    source: str = Query(""),
    min_beds: str | None = Query(None),
    max_beds: str = Query(""),
    min_price: str = Query(""),
    max_price: str = Query(""),
    area: str = Query(""),
    furnished: str = Query(""),
    bills: str = Query(""),
    tenancy_start_from: str = Query(""),
    tenancy_start_to: str = Query(""),
    available_now: str = Query(""),
    q: str = Query(""),
    sort: str = Query("newest"),
    limit: str = Query("100"),
    routes: list[str] = Query(default=[]),
    max_stop_distance_m: str | None = Query(None),
    route_mode: str = Query("filter"),
):
    settings = get_settings()
    if min_beds is None:
        min_beds = str(settings.default_beds)
    stmt = _apply_filters(
        select(Listing),
        status=status,
        source=source,
        min_beds=_to_int(min_beds),
        max_beds=_to_int(max_beds),
        min_price=_to_int(min_price),
        max_price=_to_int(max_price),
        area=area,
        furnished={"yes": True, "no": False}.get(furnished),
        bills={"yes": True, "no": False}.get(bills),
        q=q,
        active_only=True,
        tenancy_start_from=tenancy_start_from,
        tenancy_start_to=tenancy_start_to,
        available_now=_to_tri_bool(available_now),
    )
    listings = db.scalars(
        _order(stmt, sort).limit(min(_to_int(limit, 100), 1000))
    ).all()
    routes_list = _parse_routes(routes)
    mode = route_mode if route_mode in {"filter", "annotate"} else "filter"
    distance = (
        settings.default_max_stop_distance_m
        if max_stop_distance_m is None
        else _to_int(max_stop_distance_m, settings.default_max_stop_distance_m)
    )
    if mode == "annotate":
        listings, info, _warning = _transit_context(
            db, list(listings), routes_list, distance, "annotate"
        )
        return [_listing_out(x, info.get(x.id)) for x in listings]
    if not routes_list:
        return [_listing_out(x) for x in listings]
    listings, info, _warning = _transit_context(
        db, list(listings), routes_list, distance, "filter"
    )
    return [_listing_out(x, info.get(x.id)) for x in listings]


@app.post("/api/sync")
def api_sync(
    min_beds: str = Query(""),
    max_price: str = Query(""),
    sources: str = Query(""),
    tenancy_start_from: str = Query(""),
    tenancy_start_to: str = Query(""),
):
    settings = get_settings()
    query = SearchQuery(
        min_beds=_to_int(min_beds, settings.default_beds),
        max_price=_to_int(max_price, settings.default_max_price),
        tenancy_start_from=tenancy_start_from,
        tenancy_start_to=tenancy_start_to,
        max_results=settings.max_results_per_source,
    )
    names = [s.strip() for s in sources.split(",") if s.strip()] or None
    run = run_sync(query, source_names=names)
    return SyncRunOut.model_validate(run)


@app.get("/api/sync-runs", response_model=list[SyncRunOut])
def api_sync_runs(db: Session = Depends(get_db), limit: int = Query(20, le=200)):
    return db.scalars(
        select(SyncRun).order_by(SyncRun.started_at.desc()).limit(limit)
    ).all()


@app.get("/api/stats")
def api_stats(db: Session = Depends(get_db)):
    counts = dict(
        db.execute(select(Listing.status, func.count()).group_by(Listing.status)).all()
    )
    by_source = dict(
        db.execute(select(Listing.source, func.count()).group_by(Listing.source)).all()
    )
    return {
        "by_status": {k.value if hasattr(k, "value") else str(k): v for k, v in counts.items()},
        "by_source": by_source,
        "total": sum(counts.values()),
    }


# --------------------------------------------------------------------------
# Saved search API
# --------------------------------------------------------------------------
def _profile_out(profile: SearchProfile) -> ProfileOut:
    try:
        params = json.loads(profile.params_json or "{}")
    except (TypeError, ValueError):
        params = {}
    return ProfileOut(
        id=profile.id,
        name=profile.name,
        params=params,
        enabled=profile.enabled,
        created_at=profile.created_at,
    )


@app.get("/api/profiles", response_model=list[ProfileOut])
def api_profiles(db: Session = Depends(get_db)):
    profiles = db.scalars(select(SearchProfile).order_by(SearchProfile.name)).all()
    return [_profile_out(p) for p in profiles]


@app.post("/api/profiles", response_model=ProfileOut, status_code=201)
def api_create_profile(payload: ProfileIn, db: Session = Depends(get_db)):
    # Validate the query fields before persisting anything.
    query_from_params(payload.params)
    existing = db.scalar(
        select(SearchProfile).where(SearchProfile.name == payload.name.strip())
    )
    if existing:
        existing.params_json = json.dumps(payload.params)
        existing.enabled = payload.enabled
        db.commit()
        db.refresh(existing)
        return _profile_out(existing)
    profile = SearchProfile(
        name=payload.name.strip(),
        params_json=json.dumps(payload.params),
        enabled=payload.enabled,
    )
    db.add(profile)
    db.commit()
    db.refresh(profile)
    return _profile_out(profile)


@app.delete("/api/profiles/{profile_id}", status_code=204)
def api_delete_profile(profile_id: int, db: Session = Depends(get_db)):
    profile = db.get(SearchProfile, profile_id)
    if profile is None:
        raise HTTPException(404, "Profile not found")
    db.delete(profile)
    db.commit()
    return None


@app.post("/api/profiles/{profile_id}/run", response_model=SyncRunOut)
def api_run_profile(profile_id: int):
    try:
        run = run_profile(profile_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    return SyncRunOut.model_validate(run)
