"""Tests for the bus-route (BODS) transit feature."""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("HF_DATABASE_URL", "sqlite:///./test_house_finder.db")
os.environ.setdefault("HF_SCHEDULER_ENABLED", "false")

from house_finder.db import Base, engine, session_scope  # noqa: E402
from house_finder.models import (  # noqa: E402
    Listing,
    TransitRoute,
    TransitRouteStop,
    TransitStop,
)
from house_finder.schemas import SearchQuery  # noqa: E402
from house_finder.transit import geo  # noqa: E402
from house_finder.transit.ingest import (  # noqa: E402
    filter_listings_by_routes,
    parse_route_targets,
)
from house_finder.transit.naptan import parse_naptan_csv  # noqa: E402
from house_finder.transit.transxchange import (  # noqa: E402
    line_names_in_file,
    parse_transxchange,
)

NS = "http://www.transxchange.org.uk/"

TXC = f"""<?xml version="1.0" encoding="utf-8"?>
<TransXChange xmlns="{NS}">
  <StopPoints>
    <AnnotatedStopPointRef>
      <StopPointRef>018000001</StopPointRef>
      <CommonName>First Stop</CommonName>
    </AnnotatedStopPointRef>
    <AnnotatedStopPointRef>
      <StopPointRef>018000002</StopPointRef>
      <CommonName>Second Stop</CommonName>
    </AnnotatedStopPointRef>
    <AnnotatedStopPointRef>
      <StopPointRef>018000003</StopPointRef>
      <CommonName>Third Stop</CommonName>
    </AnnotatedStopPointRef>
  </StopPoints>
  <JourneyPatternSections>
    <JourneyPatternSection id="JPS1">
      <JourneyPatternTimingLink id="JPTL1">
        <From><StopPointRef>018000001</StopPointRef></From>
        <To><StopPointRef>018000002</StopPointRef></To>
      </JourneyPatternTimingLink>
      <JourneyPatternTimingLink id="JPTL2">
        <From><StopPointRef>018000002</StopPointRef></From>
        <To><StopPointRef>018000003</StopPointRef></To>
      </JourneyPatternTimingLink>
    </JourneyPatternSection>
  </JourneyPatternSections>
  <Services>
    <Service>
      <ServiceCode>PF0000001:1</ServiceCode>
      <Lines>
        <Line id="OP:PF0000001:1:U1"><LineName>U1</LineName></Line>
        <Line id="OP:PF0000001:1:U2"><LineName>U2</LineName></Line>
      </Lines>
      <StandardService>
        <JourneyPattern id="JP1">
          <RouteRef>RT1</RouteRef>
          <JourneyPatternSectionRefs>JPS1</JourneyPatternSectionRefs>
        </JourneyPattern>
      </StandardService>
    </Service>
  </Services>
  <VehicleJourneys>
    <VehicleJourney>
      <VehicleJourneyCode>VJ1</VehicleJourneyCode>
      <LineRef>OP:PF0000001:1:U1</LineRef>
      <JourneyPatternRef>JP1</JourneyPatternRef>
    </VehicleJourney>
  </VehicleJourneys>
</TransXChange>
"""


@pytest.fixture(autouse=True)
def _fresh_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)


# --------------------------------------------------------------------------
# TransXChange parsing
# --------------------------------------------------------------------------
def test_parse_transxchange_extracts_line_stop_sequence():
    parsed = parse_transxchange(TXC.encode("utf-8"), target_lines=["U1"])
    assert "U1" in parsed.lines
    assert parsed.lines["U1"].stops == ["018000001", "018000002", "018000003"]
    # Only the requested line is extracted.
    assert "U2" not in parsed.lines
    assert parsed.stops["018000001"].name == "First Stop"


def test_parse_transxchange_all_lines_when_unfiltered():
    parsed = parse_transxchange(TXC.encode("utf-8"))
    assert set(parsed.lines) == {"U1"}
    assert parsed.all_line_names == {"U1", "U2"}


def test_line_names_in_file_scanner():
    assert line_names_in_file(TXC.encode("utf-8")) == {"U1", "U2"}


def test_parse_route_targets_handles_operator_suffix():
    targets = parse_route_targets(["U1", "20@Big Lemon"])
    assert targets[0].line_norm == "U1"
    assert targets[0].operator is None
    assert targets[1].line == "20"
    assert targets[1].operator == "big lemon"


# --------------------------------------------------------------------------
# NaPTAN + geometry
# --------------------------------------------------------------------------
def test_parse_naptan_csv():
    text = (
        "ATCOCode,CommonName,Indicator,LocalityName,Longitude,Latitude\n"
        "018000001,First Stop,opp,Bath,-2.36,51.38\n"
        "018000002,No Coords,,,-,-\n"
    )
    stops = parse_naptan_csv(text)
    assert stops["018000001"].name == "First Stop"
    assert stops["018000001"].latitude == pytest.approx(51.38)
    assert "018000002" not in stops


def test_haversine_is_reasonable():
    # ~111 m per 0.001 degree of latitude.
    d = geo.haversine_m(51.380, -2.360, 51.381, -2.360)
    assert 100 < d < 120


# --------------------------------------------------------------------------
# Schemas / profiles
# --------------------------------------------------------------------------
def test_search_query_parses_routes_from_string():
    q = SearchQuery(routes="U1,U2", max_stop_distance_m="600")
    assert q.routes == ["U1", "U2"]
    assert q.max_stop_distance_m == 600
    assert "U1,U2" in q.cache_key()


# --------------------------------------------------------------------------
# Route proximity filtering
# --------------------------------------------------------------------------
def _seed_route():
    with session_scope() as session:
        stop = TransitStop(
            atco_code="018000001",
            name="Stop A",
            latitude=51.3800,
            longitude=-2.3600,
            area_code="018",
        )
        session.add(stop)
        route = TransitRoute(
            key="U1@Test Operator", line_name="U1", operator="Test Operator", stop_count=1
        )
        session.add(route)
        session.flush()
        session.add(
            TransitRouteStop(route_id=route.id, stop_id=stop.id, sequence=0)
        )

        near = Listing(
            source="rightmove",
            source_id="NEAR",
            url="https://example.com/near",
            latitude=51.3805,
            longitude=-2.3600,
            title="near",
        )
        far = Listing(
            source="rightmove",
            source_id="FAR",
            url="https://example.com/far",
            latitude=51.5000,
            longitude=-2.3600,
            title="far",
        )
        nowhere = Listing(
            source="rightmove",
            source_id="NOCOORD",
            url="https://example.com/nocoord",
            title="no coords",
        )
        session.add_all([near, far, nowhere])
        session.flush()


def test_filter_by_routes_matches_line_name_and_distance():
    _seed_route()
    with session_scope() as session:
        listings = session.query(Listing).all()
        filtered, info, warning = filter_listings_by_routes(session, listings, ["U1"], 500)
        ids = {x.source_id for x in filtered}
        assert ids == {"NEAR"}
        assert info[filtered[0].id]["route"] == "U1"
        assert info[filtered[0].id]["distance_m"] < 100
        assert warning == ""


def test_filter_by_routes_full_key_and_unknown_token():
    _seed_route()
    with session_scope() as session:
        listings = session.query(Listing).all()
        filtered, _info, _w = filter_listings_by_routes(
            session, listings, ["U1@Test Operator"], 500
        )
        assert {x.source_id for x in filtered} == {"NEAR"}

        # A token that resolves to nothing leaves listings untouched but warns.
        filtered, info, warning = filter_listings_by_routes(
            session, listings, ["NOPE"], 500
        )
        assert len(filtered) == len(listings)
        assert "NOPE" in warning


def test_profile_apply_qs_includes_routes():
    import json

    from house_finder.models import SearchProfile
    from house_finder.profiles import profile_apply_qs

    profile = SearchProfile(
        name="bus",
        params_json=json.dumps(
            {"routes": ["U1"], "max_stop_distance_m": 600, "min_beds": 6}
        ),
    )
    qs = profile_apply_qs(profile)
    assert "routes=U1" in qs
    assert "max_stop_distance_m=600" in qs
    assert "min_beds=6" in qs


def test_api_listings_applies_route_filter():
    from fastapi.testclient import TestClient

    from house_finder.app import app

    _seed_route()
    with TestClient(app) as client:
        resp = client.get(
            "/api/listings",
            params={"routes": "U1", "max_stop_distance_m": "500", "min_beds": ""},
        )
        assert resp.status_code == 200
        rows = resp.json()
        assert {x["source_id"] for x in rows} == {"NEAR"}
        assert rows[0]["nearest_route"] == "U1"
        assert rows[0]["nearest_stop_m"] is not None

        routes = client.get("/api/transit/routes").json()
        assert routes["count"] == 1

