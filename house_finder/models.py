"""SQLAlchemy ORM models."""

from __future__ import annotations

import enum
from datetime import date, datetime, timezone

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ListingStatus(str, enum.Enum):
    NEW = "new"
    SEEN = "seen"
    SHORTLISTED = "shortlisted"
    HIDDEN = "hidden"


class SyncStatus(str, enum.Enum):
    RUNNING = "running"
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"


class Listing(Base):
    __tablename__ = "listings"
    __table_args__ = (
        UniqueConstraint("source", "source_id", name="uq_listing_source_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)

    # Provenance
    source: Mapped[str] = mapped_column(String(40), index=True)
    source_id: Mapped[str] = mapped_column(String(200))
    url: Mapped[str] = mapped_column(Text)

    # Core facts
    title: Mapped[str] = mapped_column(Text, default="")
    price_pcm: Mapped[int | None] = mapped_column(Integer, index=True)
    price_pppw: Mapped[float | None] = mapped_column(Float)
    beds: Mapped[int | None] = mapped_column(Integer, index=True)
    baths: Mapped[int | None] = mapped_column(Integer)
    property_type: Mapped[str] = mapped_column(String(60), default="")
    address: Mapped[str] = mapped_column(Text, default="")
    area: Mapped[str] = mapped_column(String(120), default="", index=True)
    postcode: Mapped[str] = mapped_column(String(16), default="", index=True)
    latitude: Mapped[float | None] = mapped_column(Float)
    longitude: Mapped[float | None] = mapped_column(Float)

    # Attributes
    furnished: Mapped[bool | None] = mapped_column(Boolean)
    bills_included: Mapped[bool | None] = mapped_column(Boolean)
    # Raw text as advertised by the source (e.g. "1st July 2026", "Now").
    available_from: Mapped[str] = mapped_column(String(40), default="")
    # Parsed tenancy start date, used for filtering/sorting. Nullable when the
    # source gives no parseable date.
    tenancy_start: Mapped[date | None] = mapped_column(Date, index=True)
    available_now: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    description: Mapped[str] = mapped_column(Text, default="")
    image_url: Mapped[str] = mapped_column(Text, default="")

    # State
    status: Mapped[ListingStatus] = mapped_column(
        Enum(ListingStatus), default=ListingStatus.NEW, index=True
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    content_hash: Mapped[str] = mapped_column(String(64), index=True, default="")
    # Cross-source deduplication: listings that share a fingerprint collapse
    # onto one canonical row; the others point at it via ``duplicate_of_id``
    # and are hidden from search results.
    dedupe_key: Mapped[str] = mapped_column(String(200), default="", index=True)
    duplicate_of_id: Mapped[int | None] = mapped_column(Integer, index=True)

    first_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
    last_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    price_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    previous_price_pcm: Mapped[int | None] = mapped_column(Integer)

    raw_json: Mapped[str] = mapped_column(Text, default="")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Listing {self.source}:{self.source_id} £{self.price_pcm}>"


class SearchProfile(Base):
    """A saved, reusable search definition."""

    __tablename__ = "search_profiles"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    params_json: Mapped[str] = mapped_column(Text, default="{}")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )


class SyncRun(Base):
    __tablename__ = "sync_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[SyncStatus] = mapped_column(
        Enum(SyncStatus), default=SyncStatus.RUNNING
    )
    sources: Mapped[str] = mapped_column(Text, default="")
    found: Mapped[int] = mapped_column(Integer, default=0)
    new: Mapped[int] = mapped_column(Integer, default=0)
    updated: Mapped[int] = mapped_column(Integer, default=0)
    errors: Mapped[str] = mapped_column(Text, default="")

    results: Mapped[list["SyncSourceResult"]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )


class SyncSourceResult(Base):
    __tablename__ = "sync_source_results"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("sync_runs.id"))
    source: Mapped[str] = mapped_column(String(40))
    found: Mapped[int] = mapped_column(Integer, default=0)
    new: Mapped[int] = mapped_column(Integer, default=0)
    ok: Mapped[bool] = mapped_column(Boolean, default=True)
    error: Mapped[str] = mapped_column(Text, default="")
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)

    run: Mapped[SyncRun] = relationship(back_populates="results")


class TransitRoute(Base):
    """A bus line served by one operator, e.g. ``U1`` by First Bus.

    ``key`` is a stable identifier of the form ``line@operator`` (or just
    ``line`` when the operator is unknown). Distances are measured to any stop
    on the route, so a route is really "the set of stops a line serves".
    """

    __tablename__ = "transit_routes"

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(160), unique=True, index=True)
    line_name: Mapped[str] = mapped_column(String(60), index=True)
    operator: Mapped[str] = mapped_column(String(120), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    stop_count: Mapped[int] = mapped_column(Integer, default=0)
    source: Mapped[str] = mapped_column(String(40), default="bods")
    dataset_ids: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )

    stops: Mapped[list["TransitRouteStop"]] = relationship(
        back_populates="route", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<TransitRoute {self.key} ({self.stop_count} stops)>"


class TransitStop(Base):
    """A bus stop with coordinates (from NaPTAN)."""

    __tablename__ = "transit_stops"

    id: Mapped[int] = mapped_column(primary_key=True)
    atco_code: Mapped[str] = mapped_column(String(24), unique=True, index=True)
    name: Mapped[str] = mapped_column(Text, default="")
    indicator: Mapped[str] = mapped_column(String(40), default="")
    locality: Mapped[str] = mapped_column(String(120), default="")
    latitude: Mapped[float] = mapped_column(Float)
    longitude: Mapped[float] = mapped_column(Float)
    area_code: Mapped[str] = mapped_column(String(6), default="")


class TransitRouteStop(Base):
    """Ordered membership of a stop on a route."""

    __tablename__ = "transit_route_stops"
    __table_args__ = (
        UniqueConstraint("route_id", "stop_id", name="uq_route_stop"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    route_id: Mapped[int] = mapped_column(
        ForeignKey("transit_routes.id"), index=True
    )
    stop_id: Mapped[int] = mapped_column(ForeignKey("transit_stops.id"), index=True)
    sequence: Mapped[int] = mapped_column(Integer, default=0)

    route: Mapped[TransitRoute] = relationship(back_populates="stops")
    stop: Mapped[TransitStop] = relationship()


class GeocodeCache(Base):
    """Cached postcode/outcode -> coordinates lookups (postcodes.io)."""

    __tablename__ = "geocode_cache"

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(24), unique=True, index=True)
    postcode: Mapped[str] = mapped_column(String(16), default="")
    latitude: Mapped[float] = mapped_column(Float)
    longitude: Mapped[float] = mapped_column(Float)
    source: Mapped[str] = mapped_column(String(40), default="postcodes.io")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
