"""Command-line entry points.

Usage:
  python -m house_finder.cli sync [--beds 6] [--max-price 3000] [--sources rightmove,zoopla]
  python -m house_finder.cli serve
  python -m house_finder.cli initdb
"""

from __future__ import annotations

import argparse
import json
import logging

from sqlalchemy import select

from .config import get_settings
from .db import init_db, session_scope
from .models import SearchProfile
from .profiles import (
    params_from_query,
    profile_sources,
    query_from_params,
    run_enabled_profiles,
    run_profile,
)
from .schemas import SearchQuery
from .sync import run_sync


def _build_query(args: argparse.Namespace) -> SearchQuery:
    settings = get_settings()
    routes_raw = getattr(args, "routes", "") or ""
    routes = [r.strip() for r in routes_raw.split(",") if r.strip()]
    return SearchQuery(
        location=getattr(args, "location", "") or settings.default_location,
        min_beds=getattr(args, "beds", 0) or settings.default_beds,
        max_beds=getattr(args, "max_beds", 0) or None,
        max_price=getattr(args, "max_price", 0) or settings.default_max_price,
        min_price=getattr(args, "min_price", 0) or settings.default_min_price,
        tenancy_start_from=getattr(args, "tenancy_start_from", "") or "",
        tenancy_start_to=getattr(args, "tenancy_start_to", "") or "",
        keywords=getattr(args, "keywords", "") or "",
        radius_miles=getattr(args, "radius", 0) or 3.0,
        routes=routes,
        max_stop_distance_m=(
            getattr(args, "max_stop_distance", 0)
            or settings.default_max_stop_distance_m
        ),
        route_mode=getattr(args, "route_mode", "") or "filter",
        max_results=getattr(args, "max_results", 0) or settings.max_results_per_source,
    )


def _source_names(args: argparse.Namespace) -> list[str] | None:
    raw = getattr(args, "sources", "") or ""
    return [s.strip() for s in raw.split(",") if s.strip()] or None


def _cmd_sync(args: argparse.Namespace) -> None:
    if args.profile:
        with session_scope() as session:
            profile = session.scalar(
                select(SearchProfile).where(SearchProfile.name == args.profile)
            )
            if profile is None:
                raise SystemExit(f"No saved search named {args.profile!r}")
            profile_id = profile.id
        run = run_profile(profile_id, notify=not args.no_notify)
    else:
        run = run_sync(
            _build_query(args),
            source_names=_source_names(args),
            notify=not args.no_notify,
        )
    print(
        f"Sync #{run.id} {run.status.value}: found={run.found} new={run.new} updated={run.updated}"
    )
    if run.errors:
        print("Errors:\n" + run.errors)


def _cmd_profiles(args: argparse.Namespace) -> None:
    if args.action == "list":
        with session_scope() as session:
            profiles = session.scalars(
                select(SearchProfile).order_by(SearchProfile.name)
            ).all()
            for p in profiles:
                print(f"{p.id}\t{'on' if p.enabled else 'off'}\t{p.name}\t{p.params_json}")
        return

    if args.action == "add":
        query = _build_query(args)
        params = params_from_query(query, sources=_source_names(args))
        with session_scope() as session:
            existing = session.scalar(
                select(SearchProfile).where(SearchProfile.name == args.name)
            )
            if existing is None:
                session.add(
                    SearchProfile(
                        name=args.name,
                        params_json=json.dumps(params),
                        enabled=True,
                    )
                )
            else:
                existing.params_json = json.dumps(params)
                existing.enabled = True
        print(f"Saved search {args.name!r} saved.")
        return

    if args.action == "remove":
        with session_scope() as session:
            profile = session.scalar(
                select(SearchProfile).where(SearchProfile.name == args.name)
            )
            if profile is None:
                raise SystemExit(f"No saved search named {args.name!r}")
            session.delete(profile)
        print(f"Saved search {args.name!r} removed.")
        return

    if args.action == "run":
        if args.name:
            with session_scope() as session:
                profile = session.scalar(
                    select(SearchProfile).where(SearchProfile.name == args.name)
                )
                if profile is None:
                    raise SystemExit(f"No saved search named {args.name!r}")
                profile_id = profile.id
            runs = [run_profile(profile_id)]
        else:
            runs = run_enabled_profiles()
        for run in runs:
            print(
                f"Sync #{run.id} {run.status.value}: found={run.found} "
                f"new={run.new} updated={run.updated}"
            )
        return

    raise SystemExit(f"Unknown profiles action {args.action!r}")


def _cmd_serve(args: argparse.Namespace) -> None:
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "house_finder.app:app",
        host=args.host or settings.host,
        port=args.port or settings.port,
        reload=args.reload,
    )


def _cmd_initdb(_: argparse.Namespace) -> None:
    init_db()
    print("Database initialised.")


def _cmd_transit(args: argparse.Namespace) -> None:
    from .transit import available_routes, sync_transit

    init_db()
    if args.action == "geocode":
        from .geocode import backfill_coordinates

        with session_scope() as session:
            filled = backfill_coordinates(session)
        print(f"Geocoded {filled} listing(s).")
        return
    if args.action == "routes":
        routes = available_routes()
        if not routes:
            print("No bus routes cached yet — run `transit sync`.")
            return
        for r in routes:
            print(
                f"{r['key']}\t{r['stop_count']} stops\t"
                f"updated {r['updated_at']:%Y-%m-%d %H:%M}"
            )
        return

    result = sync_transit(force=getattr(args, "force", False))
    print(result.summary())
    for err in result.errors:
        print(f"  ! {err}")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(prog="house_finder")
    sub = parser.add_subparsers(dest="command", required=True)

    p_sync = sub.add_parser("sync", help="Run a sync across sources")
    p_sync.add_argument("--beds", type=int, default=0)
    p_sync.add_argument("--max-beds", type=int, default=0)
    p_sync.add_argument("--min-price", type=int, default=0)
    p_sync.add_argument("--max-price", type=int, default=0)
    p_sync.add_argument("--max-results", type=int, default=0)
    p_sync.add_argument("--location", type=str, default="")
    p_sync.add_argument("--keywords", type=str, default="")
    p_sync.add_argument("--radius", type=float, default=0)
    p_sync.add_argument("--routes", type=str, default="", help="Comma-separated bus route tokens, e.g. U1,U2")
    p_sync.add_argument("--max-stop-distance", type=int, default=0, help="Max metres from a selected-route stop")
    p_sync.add_argument("--route-mode", type=str, choices=["filter", "annotate"], default="filter")
    p_sync.add_argument("--tenancy-start-from", type=str, default="", help="Earliest move-in date (ISO)")
    p_sync.add_argument("--tenancy-start-to", type=str, default="", help="Latest move-in date (ISO)")
    p_sync.add_argument("--sources", type=str, default="")
    p_sync.add_argument("--profile", type=str, default="", help="Run a saved search by name")
    p_sync.add_argument("--no-notify", action="store_true")
    p_sync.set_defaults(func=_cmd_sync)

    p_profiles = sub.add_parser("profiles", help="Manage saved searches")
    p_profiles.add_argument("action", choices=["list", "add", "remove", "run"])
    p_profiles.add_argument("--name", type=str, default="")
    p_profiles.add_argument("--beds", type=int, default=0)
    p_profiles.add_argument("--max-beds", type=int, default=0)
    p_profiles.add_argument("--min-price", type=int, default=0)
    p_profiles.add_argument("--max-price", type=int, default=0)
    p_profiles.add_argument("--max-results", type=int, default=0)
    p_profiles.add_argument("--location", type=str, default="")
    p_profiles.add_argument("--keywords", type=str, default="")
    p_profiles.add_argument("--radius", type=float, default=0)
    p_profiles.add_argument("--routes", type=str, default="", help="Comma-separated bus route tokens")
    p_profiles.add_argument("--max-stop-distance", type=int, default=0)
    p_profiles.add_argument("--route-mode", type=str, choices=["filter", "annotate"], default="filter")
    p_profiles.add_argument("--tenancy-start-from", type=str, default="")
    p_profiles.add_argument("--tenancy-start-to", type=str, default="")
    p_profiles.add_argument("--sources", type=str, default="")
    p_profiles.set_defaults(func=_cmd_profiles)

    p_serve = sub.add_parser("serve", help="Run the web server")
    p_serve.add_argument("--host", type=str, default="")
    p_serve.add_argument("--port", type=int, default=0)
    p_serve.add_argument("--reload", action="store_true")
    p_serve.set_defaults(func=_cmd_serve)

    p_init = sub.add_parser("initdb", help="Create database tables")
    p_init.set_defaults(func=_cmd_initdb)

    p_transit = sub.add_parser("transit", help="Manage bus-route (BODS) data")
    p_transit.add_argument("action", choices=["sync", "routes", "geocode"])
    p_transit.add_argument("--force", action="store_true", help="Re-download the NaPTAN cache")
    p_transit.set_defaults(func=_cmd_transit)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
