"""Tests for normalisation, pipeline dedupe, and the API."""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("HF_DATABASE_URL", "sqlite:///./test_house_finder.db")
os.environ.setdefault("HF_SCHEDULER_ENABLED", "false")

from house_finder import normalize  # noqa: E402
from house_finder.db import Base, engine, session_scope  # noqa: E402
from house_finder.models import Listing, ListingStatus  # noqa: E402
from house_finder.pipeline import ingest  # noqa: E402
from house_finder.schemas import RawListing  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)


def test_parse_price():
    assert normalize.parse_price("£2,400 pcm") == 2400
    assert normalize.parse_price("2400") == 2400
    assert normalize.parse_price("") is None


def test_parse_beds():
    assert normalize.parse_beds("6 bed house") == 6
    assert normalize.parse_beds("7 bedrooms") == 7
    assert normalize.parse_beds("") is None


def test_postcode_and_area():
    assert normalize.parse_postcode("12 Oldfield Park, Bath BA2 3AB") == "BA2 3AB"
    assert normalize.detect_area("Oldfield Park, Bath") == "Oldfield Park"


def test_pppw():
    # £2400 pcm / 6 people -> ~£92.31 pp/pw
    assert normalize.price_per_person_per_week(2400, 6) == pytest.approx(92.31, abs=0.01)
    assert normalize.price_per_person_per_week(None, 6) is None


def test_parse_date_formats():
    from datetime import date

    assert normalize.parse_date("2026-07-01") == date(2026, 7, 1)
    assert normalize.parse_date("2026-07") == date(2026, 7, 1)
    assert normalize.parse_date("01/07/2026") == date(2026, 7, 1)
    assert normalize.parse_date("1st July 2026") == date(2026, 7, 1)
    assert normalize.parse_date("July 2026") == date(2026, 7, 1)
    assert normalize.parse_date("Now") is None
    assert normalize.parse_date("") is None
    assert normalize.normalise_date("1 July 2026") == "2026-07-01"


def test_available_now_detection():
    assert normalize.is_available_now("Available now")
    assert normalize.is_available_now("ASAP")
    assert not normalize.is_available_now("Available 1 July 2026")


def test_ingest_derives_tenancy_start():
    from datetime import date

    raw = RawListing(
        source="rightmove",
        source_id="TS1",
        url="https://example.com/ts1",
        title="6 bed house",
        price_pcm=2400,
        beds=6,
        available_from="1st July 2026",
    )
    with session_scope() as s:
        ingest(s, [raw])
    with session_scope() as s:
        listing = s.query(Listing).filter_by(source_id="TS1").one()
        assert listing.tenancy_start == date(2026, 7, 1)
        assert listing.available_now is False

    raw_now = raw.model_copy(
        update={"source_id": "TS2", "available_from": "Available now"}
    )
    with session_scope() as s:
        ingest(s, [raw_now])
    with session_scope() as s:
        listing = s.query(Listing).filter_by(source_id="TS2").one()
        assert listing.tenancy_start is None
        assert listing.available_now is True


def test_ingest_new_then_update():
    raw = RawListing(
        source="rightmove",
        source_id="X1",
        url="https://example.com/x1",
        title="6 bed house",
        price_pcm=2400,
        beds=6,
        address="1 Oldfield Park, Bath BA2 3AB",
    )
    with session_scope() as s:
        stats = ingest(s, [raw])
    assert stats.new == 1

    # Same listing again -> not new.
    with session_scope() as s:
        stats2 = ingest(s, [raw])
    assert stats2.new == 0

    # Price change -> tracked.
    raw2 = raw.model_copy(update={"price_pcm": 2300})
    with session_scope() as s:
        stats3 = ingest(s, [raw2])
    assert stats3.price_changes == 1
    with session_scope() as s:
        listing = s.query(Listing).filter_by(source_id="X1").one()
        assert listing.previous_price_pcm == 2400
        assert listing.price_pcm == 2300


def test_ingest_dedupes_within_batch():
    """Some sources return the same listing twice in one page."""
    raw = RawListing(
        source="zoopla",
        source_id="Z9",
        url="https://example.com/z9",
        title="6 bed house",
        price_pcm=2000,
        beds=6,
    )
    with session_scope() as s:
        stats = ingest(s, [raw, raw, raw])
    assert stats.found == 1
    assert stats.new == 1


def test_api_health_and_listings():
    from fastapi.testclient import TestClient

    from house_finder.app import app

    with TestClient(app) as client:
        assert client.get("/api/health").json()["status"] == "ok"
        assert client.get("/api/sources").status_code == 200
        assert client.get("/api/listings").status_code == 200
        assert client.get("/").status_code == 200


def test_empty_query_params_are_tolerated():
    """The HTMX filter form submits empty strings for optional numeric fields."""
    from fastapi.testclient import TestClient

    from house_finder.app import app

    with TestClient(app) as client:
        resp = client.get(
            "/partials/listings",
            params={
                "q": "",
                "min_beds": "6",
                "max_beds": "",
                "min_price": "",
                "max_price": "",
                "area": "",
                "source": "",
                "status": "",
                "furnished": "",
                "bills": "",
                "sort": "price_asc",
                "active_only": "true",
            },
        )
        assert resp.status_code == 200
        assert "listing(s)" in resp.text or "No listings" in resp.text


def test_tenancy_start_filter():
    """The tenancy start window filters listings by parsed move-in date."""
    from fastapi.testclient import TestClient

    from house_finder.app import app

    raws = [
        RawListing(
            source="rightmove",
            source_id="T-JUL",
            url="https://example.com/jul",
            title="6 bed house",
            price_pcm=2400,
            beds=6,
            available_from="2026-07-01",
        ),
        RawListing(
            source="rightmove",
            source_id="T-SEP",
            url="https://example.com/sep",
            title="6 bed house",
            price_pcm=2400,
            beds=6,
            available_from="2026-09-01",
        ),
        RawListing(
            source="rightmove",
            source_id="T-NOW",
            url="https://example.com/now",
            title="6 bed house",
            price_pcm=2400,
            beds=6,
            available_from="Available now",
        ),
    ]
    with session_scope() as s:
        ingest(s, raws)

    with TestClient(app) as client:
        # Window covering only July.
        resp = client.get(
            "/api/listings",
            params={"tenancy_start_from": "2026-06-01", "tenancy_start_to": "2026-08-01"},
        )
        ids = {x["source_id"] for x in resp.json()}
        assert ids == {"T-JUL"}

        # Open-ended from September.
        resp = client.get("/api/listings", params={"tenancy_start_from": "2026-09-01"})
        assert {x["source_id"] for x in resp.json()} == {"T-SEP"}

        # Available now only.
        resp = client.get("/api/listings", params={"available_now": "yes"})
        assert {x["source_id"] for x in resp.json()} == {"T-NOW"}

        # No filter -> all three.
        resp = client.get("/api/listings")
        assert {x["source_id"] for x in resp.json()} >= {"T-JUL", "T-SEP", "T-NOW"}


def test_tenancy_sort_and_partial():
    from fastapi.testclient import TestClient

    from house_finder.app import app

    with TestClient(app) as client:
        resp = client.get("/partials/listings", params={"sort": "tenancy_asc"})
        assert resp.status_code == 200
        resp = client.get(
            "/partials/listings",
            params={"tenancy_start_from": "2026-07-01", "tenancy_start_to": ""},
        )
        assert resp.status_code == 200


def test_status_update_returns_card_for_htmx():
    raw = RawListing(
        source="rightmove",
        source_id="HX1",
        url="https://example.com/hx1",
        title="6 bed house",
        price_pcm=2100,
        beds=6,
    )
    with session_scope() as s:
        ingest(s, [raw])
        listing_id = s.query(Listing).filter_by(source_id="HX1").one().id

    from fastapi.testclient import TestClient

    from house_finder.app import app

    with TestClient(app) as client:
        resp = client.post(
            f"/listings/{listing_id}/status",
            data={"status": "shortlisted"},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        assert f'id="listing-{listing_id}"' in resp.text
        assert "shortlisted" in resp.text

    with session_scope() as s:
        assert s.get(Listing, listing_id).status == ListingStatus.SHORTLISTED
