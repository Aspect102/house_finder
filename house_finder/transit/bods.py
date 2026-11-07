"""Client for the Bus Open Data Service (BODS) timetable API.

The API is documented (loosely) at https://data.bus-data.dft.gov.uk/api/ and
authenticates with an ``api_key`` query parameter. Only the two calls we need
are implemented: list datasets, and download one dataset's timetable archive.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import httpx

log = logging.getLogger(__name__)

BODS_BASE = "https://data.bus-data.dft.gov.uk"
LIST_PATH = "/api/v1/dataset/"
DOWNLOAD_PATH = "/timetable/dataset/{dataset_id}/download/"


class BodsError(RuntimeError):
    """Raised when the BODS API cannot satisfy a request."""


@dataclass
class DatasetMeta:
    id: int
    operator_name: str = ""
    name: str = ""
    description: str = ""
    lines: list[str] = field(default_factory=list)
    nocs: list[str] = field(default_factory=list)
    admin_areas: list[str] = field(default_factory=list)
    url: str = ""
    extension: str = ""
    status: str = ""
    bods_compliance: bool | None = None

    @classmethod
    def from_api(cls, data: dict) -> "DatasetMeta":
        return cls(
            id=int(data["id"]),
            operator_name=data.get("operatorName") or "",
            name=data.get("name") or "",
            description=data.get("description") or "",
            lines=list(data.get("lines") or []),
            nocs=list(data.get("noc") or []),
            admin_areas=[
                a.get("atco_code", "")
                for a in (data.get("adminAreas") or [])
                if isinstance(a, dict)
            ],
            url=data.get("url") or "",
            extension=data.get("extension") or "",
            status=data.get("status") or "",
            bods_compliance=data.get("bodsCompliance"),
        )


class BodsClient:
    def __init__(self, api_key: str, *, base_url: str = BODS_BASE, timeout: int = 60):
        if not api_key:
            raise BodsError("No BODS API key configured (HF_BODS_API_KEY)")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _get(self, path: str, params: dict | None = None) -> httpx.Response:
        merged = dict(params or {})
        merged["api_key"] = self.api_key
        try:
            resp = httpx.get(
                self.base_url + path,
                params=merged,
                timeout=self.timeout,
                follow_redirects=True,
            )
        except httpx.HTTPError as exc:
            raise BodsError(f"BODS request failed: {exc}") from exc
        if resp.status_code != 200:
            detail = resp.text[:200]
            raise BodsError(f"BODS HTTP {resp.status_code}: {detail}")
        return resp

    def list_datasets(
        self,
        *,
        admin_areas: list[str] | None = None,
        status: str = "published",
        page_size: int = 100,
        max_pages: int = 50,
    ) -> list[DatasetMeta]:
        """List timetable datasets, optionally filtered by ATCO admin area."""
        areas = [a for a in (admin_areas or []) if a]
        if not areas:
            areas = [None]  # single unfiltered pass

        found: dict[int, DatasetMeta] = {}
        for area in areas:
            offset = 0
            for _ in range(max_pages):
                params = {
                    "limit": page_size,
                    "offset": offset,
                    "status": status,
                }
                if area:
                    params["adminArea"] = area
                resp = self._get(LIST_PATH, params)
                payload = resp.json()
                results = payload.get("results") or []
                for item in results:
                    meta = DatasetMeta.from_api(item)
                    found[meta.id] = meta
                if not payload.get("next") or not results:
                    break
                offset += page_size
        return sorted(found.values(), key=lambda d: d.id)

    def download_dataset(self, dataset_id: int) -> bytes:
        """Download a dataset archive (zip) or XML file as raw bytes."""
        resp = self._get(DOWNLOAD_PATH.format(dataset_id=dataset_id))
        return resp.content
