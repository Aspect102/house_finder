"""Saved search profiles: serialise a SearchQuery and run profiles on schedule."""

from __future__ import annotations

import json
import logging
from urllib.parse import urlencode

from sqlalchemy import select

from .db import session_scope
from .models import SearchProfile
from .schemas import SearchQuery
from .sync import run_sync

log = logging.getLogger(__name__)

_QUERY_FIELDS = set(SearchQuery.model_fields)


def query_from_params(params: dict) -> SearchQuery:
    """Build a SearchQuery from a stored profile dict, ignoring unknown keys."""
    cleaned = {k: v for k, v in params.items() if k in _QUERY_FIELDS}
    return SearchQuery(**cleaned)


def params_from_query(query: SearchQuery, *, sources: list[str] | None = None) -> dict:
    """Return a JSON-safe dict for a profile."""
    params = query.model_dump()
    if sources:
        params["sources"] = sources
    return params


def profile_sources(params: dict) -> list[str] | None:
    raw = params.get("sources")
    if not raw:
        return None
    if isinstance(raw, str):
        return [s.strip() for s in raw.split(",") if s.strip()]
    return list(raw)


def profile_apply_qs(profile: SearchProfile) -> str:
    """A querystring that re-applies a profile's filters in the web UI."""
    try:
        params = json.loads(profile.params_json or "{}")
    except (TypeError, ValueError):
        params = {}
    view = {}
    mapping = {
        "min_beds": params.get("min_beds"),
        "max_beds": params.get("max_beds"),
        "min_price": params.get("min_price"),
        "max_price": params.get("max_price"),
        "tenancy_start_from": params.get("tenancy_start_from"),
        "tenancy_start_to": params.get("tenancy_start_to"),
        "q": params.get("keywords"),
        "furnished": params.get("furnished"),
        "bills": params.get("bills_included"),
        "available_now": params.get("available_now"),
    }
    for key, value in mapping.items():
        if value is None or value == "":
            continue
        if isinstance(value, bool):
            view[key] = "yes" if value else "no"
        elif isinstance(value, int) and value == 0:
            continue
        else:
            view[key] = str(value)
    routes = params.get("routes") or []
    if isinstance(routes, str):
        routes = [r.strip() for r in routes.split(",") if r.strip()]
    if routes:
        view["routes"] = ",".join(str(r) for r in routes)
    distance = params.get("max_stop_distance_m")
    if distance:
        view["max_stop_distance_m"] = str(distance)
    sources = params.get("sources") or []
    if isinstance(sources, (list, tuple)) and len(sources) == 1:
        view["source"] = str(sources[0])
    return urlencode(view)


def run_profile(profile_id: int, *, notify: bool = True):
    """Run one saved profile and return its SyncRun."""
    with session_scope() as session:
        profile = session.get(SearchProfile, profile_id)
        if profile is None:
            raise KeyError(f"Profile {profile_id} not found")
        params = json.loads(profile.params_json or "{}")
        name = profile.name
    query = query_from_params(params)
    log.info("running saved search %r (%s)", name, query.cache_key()[:60])
    return run_sync(
        query,
        source_names=profile_sources(params),
        notify=notify,
    )


def run_enabled_profiles() -> list:
    """Run every enabled profile. Falls back to the default query if none."""
    with session_scope() as session:
        profiles = list(
            session.scalars(
                select(SearchProfile)
                .where(SearchProfile.enabled.is_(True))
                .order_by(SearchProfile.id)
            )
        )
        ids = [(p.id, p.name) for p in profiles]

    if not ids:
        log.info("no saved searches; running default sync")
        return [run_sync()]

    runs = []
    for profile_id, name in ids:
        try:
            runs.append(run_profile(profile_id))
        except Exception:  # pragma: no cover - one bad profile shouldn't stop all
            log.exception("saved search %r failed", name)
    return runs
