"""Pydantic schemas: the cross-source search query and raw listing contract."""

from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, Field, field_validator


class SearchQuery(BaseModel):
    """A source-agnostic search. Adapters translate this to their own params."""

    location: str = "Bath, UK"
    min_beds: int = 6
    max_beds: int | None = None
    min_price: int = 0
    max_price: int = 0  # 0 == no limit
    property_type: str = ""  # house / flat / any
    furnished: bool | None = None
    bills_included: bool | None = None
    # Tenancy start date window. ``tenancy_start_from`` is the earliest
    # acceptable move-in date; ``tenancy_start_to`` the latest. ISO strings.
    tenancy_start_from: str = ""
    tenancy_start_to: str = ""
    available_now: bool | None = None  # True == only "available now"
    keywords: str = ""
    radius_miles: float = 3.0
    max_results: int = 60
    # Bus-route proximity. ``routes`` holds selected route tokens (a line name
    # such as "U1" or a full route key such as "U1@First Bus");
    # ``max_stop_distance_m`` is the walking cutoff (0 == no limit).
    routes: list[str] = Field(default_factory=list)
    max_stop_distance_m: int = 800

    @field_validator("routes", mode="before")
    @classmethod
    def _split_routes(cls, value: object) -> object:
        if value in ("", None):
            return []
        if isinstance(value, str):
            return [s.strip() for s in value.split(",") if s.strip()]
        return value

    @field_validator("max_stop_distance_m", mode="before")
    @classmethod
    def _blank_distance(cls, v: object) -> object:
        if v in ("", None):
            return 0
        return v

    @field_validator("min_beds", "min_price", "max_price", mode="before")
    @classmethod
    def _blank_to_zero(cls, v: object) -> object:
        if v in ("", None):
            return 0
        return v

    @field_validator("tenancy_start_from", "tenancy_start_to", mode="before")
    @classmethod
    def _normalise_date(cls, v: object) -> object:
        """Accept ISO dates or free text; store as ISO ``YYYY-MM-DD``."""
        if v in ("", None):
            return ""
        from .normalize import normalise_date

        return normalise_date(str(v)) or str(v).strip()

    def tenancy_start_date(self) -> date | None:
        from .normalize import parse_date

        return parse_date(self.tenancy_start_from)

    def tenancy_end_date(self) -> date | None:
        from .normalize import parse_date

        return parse_date(self.tenancy_start_to)

    def cache_key(self) -> str:
        return "|".join(
            str(x)
            for x in (
                self.location,
                self.min_beds,
                self.max_beds,
                self.min_price,
                self.max_price,
                self.property_type,
                self.furnished,
                self.bills_included,
                self.tenancy_start_from,
                self.tenancy_start_to,
                self.available_now,
                self.keywords,
                self.radius_miles,
                ",".join(self.routes),
                self.max_stop_distance_m,
            )
        )


class RawListing(BaseModel):
    """Normalised-ish listing emitted by a source adapter.

    Adapters fill what they can; the pipeline normalises the rest.
    """

    source: str
    source_id: str
    url: str
    title: str = ""
    price_pcm: int | None = None
    beds: int | None = None
    baths: int | None = None
    property_type: str = ""
    address: str = ""
    area: str = ""
    postcode: str = ""
    latitude: float | None = None
    longitude: float | None = None
    furnished: bool | None = None
    bills_included: bool | None = None
    available_from: str = ""
    tenancy_start: str = ""  # ISO date, parsed by the pipeline if absent
    available_now: bool | None = None
    description: str = ""
    image_url: str = ""
    raw: dict = Field(default_factory=dict)


class ListingOut(BaseModel):
    id: int
    source: str
    source_id: str
    url: str
    title: str
    price_pcm: int | None
    price_pppw: float | None
    beds: int | None
    baths: int | None
    property_type: str
    address: str
    area: str
    postcode: str
    latitude: float | None = None
    longitude: float | None = None
    furnished: bool | None
    bills_included: bool | None
    available_from: str
    tenancy_start: date | None
    available_now: bool
    description: str
    image_url: str
    status: str
    is_active: bool
    first_seen: datetime
    last_seen: datetime
    previous_price_pcm: int | None
    # Populated only when a bus-route filter is applied.
    nearest_route: str | None = None
    nearest_stop: str | None = None
    nearest_stop_m: int | None = None

    @field_validator("status", mode="before")
    @classmethod
    def _status_value(cls, value: object) -> object:
        return getattr(value, "value", value)

    model_config = {"from_attributes": True}


class SyncRunOut(BaseModel):
    id: int
    started_at: datetime
    finished_at: datetime | None
    status: str
    sources: str
    found: int
    new: int
    updated: int
    errors: str

    @field_validator("status", mode="before")
    @classmethod
    def _status_value(cls, value: object) -> object:
        return getattr(value, "value", value)

    model_config = {"from_attributes": True}


class ProfileIn(BaseModel):
    """A saved search supplied through the REST API."""

    name: str = Field(min_length=1, max_length=120)
    params: dict = Field(default_factory=dict)
    enabled: bool = True


class ProfileOut(BaseModel):
    id: int
    name: str
    params: dict
    enabled: bool
    created_at: datetime

    model_config = {"from_attributes": True}
