"""Geodesic distance helpers used for bus-stop proximity filtering."""

from __future__ import annotations

import math
from typing import Iterable

EARTH_RADIUS_M = 6_371_008.8


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two WGS84 points, in metres."""
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = (
        math.sin(dphi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    )
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(a)))


def nearest_point(lat: float, lon: float, points: Iterable):
    """Return ``(point, distance_m)`` for the closest point, or ``(None, None)``.

    Each ``point`` must expose ``.latitude`` and ``.longitude``.
    """
    best = None
    best_d = None
    for point in points:
        if point.latitude is None or point.longitude is None:
            continue
        d = haversine_m(lat, lon, point.latitude, point.longitude)
        if best_d is None or d < best_d:
            best = point
            best_d = d
    return best, best_d
