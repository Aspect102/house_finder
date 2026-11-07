"""Tests for saved searches, price normalisation, defaults and parsers."""

from __future__ import annotations

import json

import os

import pytest

os.environ.setdefault("HF_DATABASE_URL", "sqlite:///./test_house_finder.db")
os.environ.setdefault("HF_SCHEDULER_ENABLED", "false")

from house_finder import normalize  # noqa: E402
from house_finder.db import Base, engine, session_scope  # noqa: E402
from house_finder.models import Listing, ListingStatus  # noqa: E402
from house_finder.pipeline import ingest  # noqa: E402
from house_finder.profiles import query_from_params  # noqa: E402
from house_finder.schemas import RawListing  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)


def _raw(**overrides) -> RawListing:
    data = {
        "source": "rightmove",
        "source_id": "F1",
        "url": "https://example.com/f1",
        "title": "6 bed house",
        "price_pcm": 4200,
        "beds": 6,
    }
    data.update(overrides)
    return RawListing(**data)


def _seed(*raws: RawListing) -> None:
    with session_scope() as session:
        ingest(session, list(raws))


# --------------------------------------------------------------------------
# Whole-property price normalisation
# --------------------------------------------------------------------------
def test_whole_property_price_leaves_whole_house_rents_alone():
    assert normalize.whole_property_price(4200, 6) == 4200
    assert normalize.whole_property_price(3000, 6) == 3000


def test_whole_property_price_multiplies_per_person_rents():
    # OnTheMarket/Zoopla often quote the per-person monthly rent.
    assert normalize.whole_property_price(901, 6) == 5406
    assert normalize.whole_property_price(780, 6) == 4680


def test_whole_property_price_ignores_rooms_and_high_figures():
    assert normalize.whole_property_price(650, 1) == 650
    assert normalize.whole_property_price(1200, 2) == 1200
    # Above the per-person cap: leave genuine (if odd) whole-house rents.
    assert normalize.whole_property_price(1575, 7) == 1575
    assert normalize.whole_property_price(None, 6) is None


def test_pipeline_stores_whole_property_price_and_pppw():
    _seed(_raw(source_id="PP1", price_pcm=901, beds=6))
    with session_scope() as session:
        listing = session.query(Listing).filter_by(source_id="PP1").one()
        assert listing.price_pcm == 5406
        assert listing.price_pppw == pytest.approx(207.92, abs=0.01)


# --------------------------------------------------------------------------
# Stats, defaults and filters
# --------------------------------------------------------------------------
def test_stats_use_string_enum_keys():
    from fastapi.testclient import TestClient

    from house_finder.app import app

    _seed(
        _raw(source_id="S1"),
        _raw(source_id="S2", title="5 bed house", beds=5, price_pcm=3000),
    )
    with session_scope() as session:
        row = session.query(Listing).filter_by(source_id="S2").one()
        row.status = ListingStatus.SHORTLISTED

    with TestClient(app) as client:
        stats = client.get("/api/stats").json()
        assert stats["by_status"].get("new") == 1
        assert stats["by_status"].get("shortlisted") == 1
        assert "ListingStatus" not in " ".join(stats["by_status"])
        page = client.get("/")
        assert "Shortlisted" in page.text
        assert 'value="6"' in page.text


def test_default_min_beds_is_six_on_first_load():
    from fastapi.testclient import TestClient

    from house_finder.app import app

    _seed(
        _raw(source_id="B6", title="6 bed house", beds=6),
        _raw(source_id="B5", title="5 bed house", beds=5),
    )
    with TestClient(app) as client:
        page = client.get("/")
        assert 'name="min_beds" min="0" value="6"' in page.text

        # The API applies the same default when no parameter is supplied.
        ids = {x["source_id"] for x in client.get("/api/listings").json()}
        assert ids == {"B6"}

        # An explicitly cleared field means "no minimum".
        ids = {
            x["source_id"]
            for x in client.get("/api/listings", params={"min_beds": ""}).json()
        }
        assert ids >= {"B6", "B5"}


# --------------------------------------------------------------------------
# Saved searches
# --------------------------------------------------------------------------
def test_profile_query_from_params_ignores_unknown_fields():
    query = query_from_params(
        {"min_beds": 6, "max_price": 6000, "sources": ["rightmove"], "bogus": 1}
    )
    assert query.min_beds == 6
    assert query.max_price == 6000


def test_saved_search_api_crud_and_apply_link():
    from fastapi.testclient import TestClient

    from house_finder.app import app

    params = {
        "min_beds": 6,
        "max_price": 6000,
        "tenancy_start_from": "2027-06-01",
        "tenancy_start_to": "2027-09-30",
        "furnished": True,
    }
    with TestClient(app) as client:
        created = client.post("/api/profiles", json={"name": "Six for 2027", "params": params})
        assert created.status_code == 201
        profile = created.json()
        assert profile["name"] == "Six for 2027"
        assert profile["params"]["min_beds"] == 6
        assert profile["enabled"] is True

        listed = client.get("/api/profiles").json()
        assert [p["name"] for p in listed] == ["Six for 2027"]

        page = client.get("/")
        assert "Six for 2027" in page.text
        assert "min_beds=6" in page.text

        # Saving the same name updates rather than duplicating.
        updated = client.post(
            "/api/profiles",
            json={"name": "Six for 2027", "params": {"min_beds": 7}, "enabled": False},
        )
        assert updated.status_code == 201
        assert updated.json()["id"] == profile["id"]
        assert updated.json()["enabled"] is False
        assert len(client.get("/api/profiles").json()) == 1

        assert client.delete(f"/api/profiles/{profile['id']}").status_code == 204
        assert client.get("/api/profiles").json() == []


def test_saved_search_form_accepts_current_filters():
    from fastapi.testclient import TestClient

    from house_finder.app import app

    with TestClient(app) as client:
        resp = client.post(
            "/profiles",
            data={
                "name": "Form profile",
                "min_beds": "6",
                "max_beds": "",
                "min_price": "",
                "max_price": "6000",
                "tenancy_start_from": "2027-06-01",
                "tenancy_start_to": "",
                "q": "student",
                "furnished": "yes",
                "bills": "",
                "source": "rightmove",
                "available_now": "",
                "routes": "U1,U2",
                "max_stop_distance_m": "500",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        profiles = client.get("/api/profiles").json()
        assert len(profiles) == 1
        stored = profiles[0]["params"]
        assert stored["min_beds"] == 6
        assert stored["furnished"] is True
        assert stored["keywords"] == "student"
        assert stored["sources"] == ["rightmove"]
        assert stored["routes"] == ["U1", "U2"]
        assert stored["max_stop_distance_m"] == 500


# --------------------------------------------------------------------------
# Offline parser fixtures
# --------------------------------------------------------------------------
def test_rightmove_parser_handles_house_share_as_single_room():
    from house_finder.sources.rightmove import RightmoveSource

    payload = {
        "props": {
            "pageProps": {
                "searchResults": {
                    "properties": [
                        {
                            "id": 111,
                            "bedrooms": 6,
                            "bathrooms": 2,
                            "propertySubType": "House Share",
                            "displayAddress": "1 Freeview Road, Bath BA2 3AB",
                            "price": {
                                "amount": 210,
                                "frequency": "weekly",
                            },
                            "letAvailableDate": "2027-07-01T00:00:00Z",
                            "propertyUrl": "/properties/111#/?channel=RES_LET",
                            "summary": "Student house, all bills included",
                        },
                        {
                            "id": 222,
                            "bedrooms": 6,
                            "bathrooms": 3,
                            "propertySubType": "House",
                            "displayAddress": "2 Oldfield Park, Bath BA2 3AB",
                            "price": {
                                "amount": 4200,
                                "frequency": "monthly",
                            },
                            "letAvailableDate": "2027-08-01",
                            "propertyUrl": "/properties/222",
                            "summary": "Furnished six bed student house",
                        },
                    ]
                }
            }
        }
    }
    html = (
        '<script id="__NEXT_DATA__" type="application/json">'
        + json.dumps(payload)
        + "</script>"
    )
    listings = RightmoveSource()._parse_next_data(html)
    assert len(listings) == 2
    room, house = listings
    assert room.beds == 1
    assert room.property_type == "Room in house share"
    assert room.price_pcm == 910
    assert house.beds == 6
    assert house.price_pcm == 4200
    assert house.tenancy_start == "2027-08-01"
    assert house.url == "https://www.rightmove.co.uk/properties/222"


def test_onthemarket_parser_reads_price_and_availability():
    from house_finder.sources.onthemarket import OnTheMarketSource

    payload = {
        "props": {
            "pageProps": {
                "initialReduxState": {
                    "results": {
                        "list": [
                            {
                                "id": 333,
                                "short-price": "£901 pcm (£208 pw)",
                                "address": "Upper Bristol Road, Bath BA1",
                                "property-title": "6 bedroom house to rent",
                                "bedrooms": 6,
                                "bathrooms": 2,
                                "humanised-property-type": "House",
                                "features": ["Student", "Furnished"],
                                "available-from": "Available from 1 August 2027",
                                "details-url": "/details/333/",
                            }
                        ]
                    }
                }
            }
        }
    }
    html = (
        '<script id="__NEXT_DATA__" type="application/json">'
        + json.dumps(payload)
        + "</script>"
    )
    listings = OnTheMarketSource()._parse_next_data(html)
    assert len(listings) == 1
    listing = listings[0]
    assert listing.source_id == "333"
    assert listing.price_pcm == 901
    assert listing.beds == 6
    assert listing.furnished is True
    assert listing.tenancy_start == "Available from 1 August 2027"


def test_unihomes_parser_converts_per_room_weekly_rent():
    from house_finder.sources.unihomes import UnihomesSource

    payload = [
        {
            "id": 408686601,
            "bedroomCount": 6,
            "bathroomCount": 2,
            "price": "196",
            "propertyName": "6 Bedroom House",
            "addressShort": "Winchester Road, Oldfield Park",
            "postcode": "BA2 3LF",
            "availableFrom": "Available from 26th July 2027",
            "availableFromShort": "Available from 26th July 2027",
            "path": "https:\\/\\/www.unihomes.co.uk\\/property\\/408686601",
            "mainImage": "https:\\/\\/cdn.example.com\\/image.jpg",
            "typeString": "house",
            "shortDescription": "6 bedroom student house in Oldfield Park",
        }
    ]
    raw = json.dumps(payload).replace('"', "\\u0022")
    html = f':initial-properties="JSON.parse(\'{raw}\')"'
    listings = UnihomesSource()._parse_embedded(html)
    assert len(listings) == 1
    listing = listings[0]
    assert listing.beds == 6
    assert listing.price_pcm == int(196 * 6 * 52 / 12)
    assert listing.area == "Oldfield Park"
    assert listing.bills_included is True
    assert listing.url == "https://www.unihomes.co.uk/property/408686601"


def test_openrent_parser_multiplies_per_person_rent():
    from house_finder.sources.openrent import OpenRentSource

    html = """
    <a href="/property-to-rent/bath/6-bed-terraced-house-brook-road-ba2/3094359"
       class="pli search-property-card text-decoration-none" id="p1">
      <img src="//staticcdn.openrent.co.uk/photo.jpg"
           alt="6 Bed Terraced House, Brook Road, BA2">
      <span>£780 per person per month</span>
      <span>Furnished</span>
      <span>6 Beds 2 Baths</span>
      <div data-listing-id="3094359"></div>
    </a>
    """
    listings = OpenRentSource()._parse_cards(html)
    assert len(listings) == 1
    listing = listings[0]
    assert listing.source_id == "3094359"
    assert listing.beds == 6
    assert listing.price_pcm == 4680
    assert listing.address == "Brook Road, BA2"


def test_openrent_detects_challenge_page():
    from house_finder.sources.openrent import _looks_blocked

    assert _looks_blocked("<title>Human Verification</title> ... captcha ...")
    assert _looks_blocked("Just a moment...")
    assert not _looks_blocked('<a class="pli search-property-card" href="/x">')


def test_spareroom_parser_reads_weekly_room_rate():
    from house_finder.sources.spareroom import SpareRoomSource

    html = """
    <li class="listing-result"
        data-listing-id="9729691"
        data-listing-url="/flatshare/somerset/bath/9729691"
        data-listing-title="Luxury Room, En Suite"
        data-listing-neighbourhood="Bath"
        data-listing-postcode="BA1 2AB"
        data-listing-rooms-in-property="6"
        data-listing-property-type="house"
        data-listing-ad-headline-rate="£145"
        data-listing-ad-headline-rate-period="pw"
        data-listing-available-now="1"></li>
    """
    listings = SpareRoomSource()._parse_cards(html)
    assert len(listings) == 1
    listing = listings[0]
    assert listing.beds == 1
    assert listing.price_pcm == int(145 * 52 / 12)
    assert listing.available_now is True
    assert listing.property_type == "Room in 6-bed house"


def test_zoopla_parser_reads_card_markup():
    from house_finder.sources.zoopla import ZooplaSource

    html = """
    <div data-testid="listing-card-content">
      <a href="/to-rent/details/74681114/"></a>
      <address>Wedgwood Road, Bath BA2</address>
      <span>£4,680 pcm</span>
      <span>6 beds</span>
      <span>2 baths</span>
      <span>Available from 1 August 2027</span>
      <img src="https://img.example/1.jpg">
    </div>
    """
    listings = ZooplaSource()._parse_cards(html)
    assert len(listings) == 1
    listing = listings[0]
    assert listing.source_id == "74681114"
    assert listing.price_pcm == 4680
    assert listing.beds == 6
    assert listing.postcode == "BA2"
    assert listing.available_from.lower().startswith("available from")
