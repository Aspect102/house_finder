"""Application configuration loaded from environment / .env."""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="HF_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        # Keep comma-separated values (e.g. HF_SOURCES=a,b) as strings so the
        # validator below can split them instead of pydantic JSON-decoding.
        enable_decoding=False,
    )

    # Database
    database_url: str = "sqlite:///./house_finder.db"

    # Web server
    host: str = "127.0.0.1"
    port: int = 8000

    # Scheduler
    scheduler_enabled: bool = True
    sync_interval_minutes: int = 180
    sync_on_startup: bool = False

    # Search defaults
    default_beds: int = 6
    default_location: str = "Bath, UK"
    default_min_price: int = 0
    default_max_price: int = 0

    # Sources. SpareRoom is room-oriented and frequently serves an anti-bot
    # challenge, so it is available but not enabled by default.
    sources: list[str] = Field(
        default_factory=lambda: [
            "rightmove",
            "unihomes",
            "openrent",
            "onthemarket",
            "zoopla",
        ]
    )

    # Scraping
    request_timeout: int = 25
    request_delay_seconds: float = 1.5
    max_results_per_source: int = 60
    # Re-use a normal desktop browser UA. Many portals return 403/Cloudflare
    # challenges to obviously automated user agents.
    user_agent: str = (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
    respect_robots: bool = True
    use_playwright_fallback: bool = True
    # Max result pages fetched per source per sync.
    max_pages_per_source: int = 6
    # Extra settle time after a browser page load (lazy-loaded cards).
    browser_settle_ms: int = 2500

    # ------------------------------------------------------------------
    # Transit (bus routes) via the Bus Open Data Service
    # ------------------------------------------------------------------
    # BODS API key. Without it the transit feature simply reports no data.
    bods_api_key: str = ""
    # Lines to ingest. A target is either "LINE" (any operator) or
    # "LINE@OPERATOR" (case-insensitive operator substring). U1N is included
    # for completeness even though First Bus does not currently publish a Bath
    # U1N timetable.
    bods_routes: list[str] = Field(
        default_factory=lambda: [
            "U1",
            "U1N",
            "U2",
            "U3",
            "U4",
            "U6",
            "20@Big Lemon",
        ]
    )
    # ATCO administrative-area codes used to filter stops. 018 is Bath &
    # North East Somerset; add neighbours (e.g. 017 South Gloucestershire) if a
    # route of interest spills over the boundary.
    bods_admin_areas: list[str] = Field(default_factory=lambda: ["018"])
    # How often the scheduler refreshes route/stop data.
    bods_refresh_hours: int = 168
    bods_timeout: int = 90
    # Where downloaded NaPTAN CSVs are cached. Empty -> <project>/data/transit.
    bods_naptan_cache_dir: str = ""
    # Default maximum walking distance from a selected-route stop, in metres.
    default_max_stop_distance_m: int = 800
    # Geocode listings that lack coordinates but have a postcode (postcodes.io).
    geocode_enabled: bool = True
    geocode_timeout: int = 10

    # Notifications
    notify_enabled: bool = False
    notify_on_new: bool = True
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_use_tls: bool = True
    email_from: str = ""
    email_to: str = ""

    webhook_url: str = ""

    @field_validator("sources", "bods_routes", "bods_admin_areas", mode="before")
    @classmethod
    def _split_list(cls, value: object) -> object:
        if isinstance(value, str):
            return [s.strip() for s in value.split(",") if s.strip()]
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
