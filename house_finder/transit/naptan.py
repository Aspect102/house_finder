"""NaPTAN stop-coordinate download.

BODS timetable datasets usually annotate stops with a reference and name but
omit coordinates. The DfT NaPTAN "access nodes" API provides them per
administrative area::

    https://naptan.api.dft.gov.uk/v1/access-nodes?atcoAreaCodes=018&dataFormat=csv

NaPTAN ATCO codes start with a three-digit administrative-area code (``018``
is Bath & North East Somerset), which lets us fetch only the areas a route
actually touches.
"""

from __future__ import annotations

import csv
import io
import logging
import os
from dataclasses import dataclass
from pathlib import Path

import httpx

log = logging.getLogger(__name__)

NAPTAN_URL = "https://naptan.api.dft.gov.uk/v1/access-nodes"


@dataclass
class NaptanStop:
    atco_code: str
    name: str
    latitude: float
    longitude: float
    indicator: str = ""
    locality: str = ""


def area_code_of(atco_code: str) -> str:
    """The three-character administrative-area prefix of an ATCO code."""
    return (atco_code or "")[:3]


def parse_naptan_csv(text: str) -> dict[str, NaptanStop]:
    """Parse a NaPTAN access-nodes CSV into ``{atco_code: NaptanStop}``."""
    stops: dict[str, NaptanStop] = {}
    reader = csv.DictReader(io.StringIO(text))
    for row in reader:
        code = (row.get("ATCOCode") or "").strip()
        if not code:
            continue
        try:
            lat = float(row.get("Latitude") or "")
            lon = float(row.get("Longitude") or "")
        except (TypeError, ValueError):
            continue
        stops[code] = NaptanStop(
            atco_code=code,
            name=(row.get("CommonName") or "").strip(),
            latitude=lat,
            longitude=lon,
            indicator=(row.get("Indicator") or "").strip(),
            locality=(row.get("LocalityName") or "").strip(),
        )
    return stops


def fetch_area_stops(
    area_code: str,
    *,
    cache_dir: str | Path | None = None,
    timeout: int = 60,
    force: bool = False,
) -> dict[str, NaptanStop]:
    """Return NaPTAN stops for an administrative area, caching the CSV on disk."""
    path: Path | None = None
    if cache_dir:
        path = Path(cache_dir) / f"naptan_{area_code}.csv"
    if path and path.exists() and not force:
        return parse_naptan_csv(path.read_text(encoding="utf-8", errors="ignore"))

    params = {"atcoAreaCodes": area_code, "dataFormat": "csv"}
    log.info("downloading NaPTAN stops for area %s", area_code)
    resp = httpx.get(NAPTAN_URL, params=params, timeout=timeout, follow_redirects=True)
    resp.raise_for_status()
    text = resp.text
    if path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return parse_naptan_csv(text)
