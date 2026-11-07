"""Transit / bus-route proximity support backed by the Bus Open Data Service.

The package is split into small, individually testable pieces:

* :mod:`~house_finder.transit.geo` — distance helpers.
* :mod:`~house_finder.transit.transxchange` — TransXChange (BODS timetable) parser.
* :mod:`~house_finder.transit.naptan` — NaPTAN stop-coordinate downloader.
* :mod:`~house_finder.transit.bods` — BODS dataset API client.
* :mod:`~house_finder.transit.ingest` — orchestration and persistence.

Everything here degrades gracefully: with no BODS API key the rest of the app
still runs and simply reports no transit data.
"""

from __future__ import annotations

from .geo import haversine_m
from .ingest import (
    TransitSyncResult,
    available_routes,
    filter_listings_by_routes,
    route_points,
    sync_transit,
)

__all__ = [
    "TransitSyncResult",
    "available_routes",
    "filter_listings_by_routes",
    "haversine_m",
    "route_points",
    "sync_transit",
]

