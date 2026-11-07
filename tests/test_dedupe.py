"""Tests for cross-source deduplication."""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("HF_DATABASE_URL", "sqlite:///./test_house_finder.db")
os.environ.setdefault("HF_SCHEDULER_ENABLED", "false")

from house_finder import normalize  # noqa: E402
from house_finder.db import Base, engine, session_scope  # noqa: E402
from house_finder.models import Listing  # noqa: E402
from house_finder.pipeline import dedupe_listings, duplicate_sources, ingest  # noqa: E402
from house_finder.schemas import RawListing  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)


def _raw(source, source_id, **overrides) -> RawListing:
    data = {
        "source": source,
        "source_id": source_id,
        "url": f"https://example.com/{source}/{source_id}",
        "title": "6 bed house",
        "price_pcm": 4680,
        "beds": 6,
        "address": "Brook Road, Bath BA2 3LF",
        "postcode": "BA2 3LF",
    }
    data.update(overrides)
    return RawListing(**data)


# --------------------------------------------------------------------------
# Fingerprint
# --------------------------------------------------------------------------
def test_fingerprint_matches_across_address_formats_and_postcode_precision():
    a = normalize.dedupe_fingerprint("Brook Road, Bath BA2 3LF", "BA2 3LF", 6, 4680)
    b = normalize.dedupe_fingerprint("6 Bed Terraced House, Brook Road, BA2", "BA2", 6, 4680)
    assert a == b != ""


def test_fingerprint_separates_different_properties():
    base = normalize.dedupe_fingerprint("Brook Road, BA2 3LF", "BA2 3LF", 6, 4680)
    assert base != normalize.dedupe_fingerprint("Brook Road, BA2 3LF", "BA2 3LF", 5, 4680)
    assert base != normalize.dedupe_fingerprint("Mill Lane, BA2 3LF", "BA2 3LF", 6, 4680)
    # Price difference beyond the bucket keeps them apart.
    assert base != normalize.dedupe_fingerprint("Brook Road, BA2 3LF", "BA2 3LF", 6, 5100)
    # Not enough information to match.
    assert normalize.dedupe_fingerprint("Brook Road", "", 6, 4680) == ""
    assert normalize.dedupe_fingerprint("", "BA2 3LF", 6, 4680) == ""


def test_postcode_outcode():
    assert normalize.postcode_outcode("BA2 3LF") == "BA2"
    assert normalize.postcode_outcode("ba23lf") == "BA2"
    assert normalize.postcode_outcode("BA2") == "BA2"
    assert normalize.postcode_outcode("SW1A 1AA") == "SW1A"


# --------------------------------------------------------------------------
# Ingest sets the key; dedupe links duplicates
# --------------------------------------------------------------------------
def test_ingest_sets_dedupe_key():
    with session_scope() as session:
        ingest(session, [_raw("rightmove", "R1")])
    with session_scope() as session:
        listing = session.query(Listing).filter_by(source_id="R1").one()
        assert listing.dedupe_key
        assert listing.duplicate_of_id is None


def test_dedupe_links_cross_source_duplicates_and_prefers_source():
    with session_scope() as session:
        ingest(
            session,
            [
                _raw("onthemarket", "O1"),
                _raw("rightmove", "R1"),  # higher priority -> canonical
            ],
        )
    with session_scope() as session:
        marked = dedupe_listings(session)
        assert marked == 1
    with session_scope() as session:
        canonical = session.query(Listing).filter_by(source="rightmove").one()
        other = session.query(Listing).filter_by(source="onthemarket").one()
        assert canonical.duplicate_of_id is None
        assert other.duplicate_of_id == canonical.id
        dup_map = duplicate_sources(session, [canonical.id])
        assert dup_map == {canonical.id: ["onthemarket"]}


def test_dedupe_does_not_merge_different_prices():
    with session_scope() as session:
        ingest(
            session,
            [
                _raw("rightmove", "R1", price_pcm=4680),
                _raw("onthemarket", "O1", price_pcm=5400),
            ],
        )
    with session_scope() as session:
        assert dedupe_listings(session) == 0
        assert session.query(Listing).count() == 2
        assert all(l.duplicate_of_id is None for l in session.query(Listing))


def test_api_hides_duplicates():
    from fastapi.testclient import TestClient

    from house_finder.app import app

    with session_scope() as session:
        ingest(
            session,
            [
                _raw("onthemarket", "O1"),
                _raw("rightmove", "R1"),
            ],
        )
        dedupe_listings(session)
    with TestClient(app) as client:
        rows = client.get("/api/listings", params={"min_beds": ""}).json()
        assert {x["source"] for x in rows} == {"rightmove"}
        stats = client.get("/api/stats").json()
        assert stats["total"] == 1
