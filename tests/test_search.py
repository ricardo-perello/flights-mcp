"""Search behavior that must not touch the network."""

import json
from typing import Any

import pytest

from flights_mcp.errors import FlightToolError
from flights_mcp.search import _Cache, run_search
from tests.support import depart_in, zurich_barcelona


def _search(**overrides: Any):
    day = overrides.pop("depart_date", depart_in(21))
    fetcher = overrides.pop("fetcher", lambda query: [zurich_barcelona(day)])
    cache = overrides.pop("cache", None)
    args = {
        "origin": "Zurich",
        "destination": "Barcelona",
        "depart_date": day,
        "fetcher": fetcher,
        "cache": cache,
    }
    args.update(overrides)
    return run_search(**args)


def test_named_cities_become_iata_and_keep_the_source_price():
    day = depart_in(21)
    result = _search(depart_date=day, fetcher=lambda query: [zurich_barcelona(day, price=108)])
    assert result.origin == "ZRH"
    assert result.destination == "BCN"
    assert result.currency == "CHF"
    assert result.adults == 1
    assert result.cabin == "economy"
    assert result.cached is False
    flight = result.flights[0]
    assert flight.price == 108
    assert flight.airlines == ["SWISS"]
    assert flight.airline_code == "LX"
    assert flight.stops == 0
    assert flight.route == ["ZRH", "BCN"]
    assert flight.depart_local.endswith("T09:30")
    assert flight.arrive_local.endswith("T11:10")
    assert flight.duration_minutes == 100
    assert flight.search_url.startswith("https://www.google.com/travel/flights")
    assert "flight_numbers" not in flight.model_dump()


def test_connection_keeps_stops_airlines_and_elapsed_time():
    day = depart_in(21)
    result = _search(depart_date=day, fetcher=lambda query: [zurich_barcelona(day, price=77, stops=True)])
    flight = result.flights[0]
    assert flight.price == 77
    assert flight.airlines == ["Air Serbia"]
    assert flight.stops == 1
    assert flight.route == ["ZRH", "BEG", "BCN"]
    assert flight.legs[0].origin == "ZRH"
    assert flight.legs[1].destination == "BCN"
    assert flight.duration_basis == "elapsed"
    assert flight.duration_minutes > flight.legs[0].duration_minutes


def test_missing_price_is_dropped_and_never_replaced():
    day = depart_in(21)
    result = _search(
        depart_date=day,
        fetcher=lambda query: [
            zurich_barcelona(day, price=None),
            zurich_barcelona(day, price=143),
        ],
    )
    assert [flight.price for flight in result.flights] == [143]
    assert any("without a numeric price" in note for note in result.notes)


def test_only_unpriced_itineraries_are_an_error():
    day = depart_in(21)
    with pytest.raises(FlightToolError) as caught:
        _search(depart_date=day, fetcher=lambda query: [zurich_barcelona(day, price=None)])
    payload = json.loads(str(caught.value))
    assert payload["code"] == "unpriced"
    assert "price" not in payload


def test_nonstop_filter_does_not_invent_a_direct_fare():
    day = depart_in(21)
    with pytest.raises(FlightToolError) as caught:
        _search(
            depart_date=day,
            nonstop=True,
            fetcher=lambda query: [zurich_barcelona(day, price=77, stops=True)],
        )
    assert json.loads(str(caught.value))["code"] == "no_results"


def test_return_before_departure_and_same_place():
    day = depart_in(21)
    earlier = depart_in(20)
    with pytest.raises(FlightToolError) as dates:
        _search(depart_date=day, return_date=earlier)
    assert json.loads(str(dates.value))["code"] == "invalid_input"

    with pytest.raises(FlightToolError) as same:
        _search(origin="ZRH", destination="Zurich")
    assert json.loads(str(same.value))["code"] == "invalid_input"


def test_identical_searches_reuse_the_process_cache():
    day = depart_in(21)
    calls = {"n": 0}

    def fetcher(query):
        calls["n"] += 1
        return [zurich_barcelona(day, price=108)]

    cache = _Cache(ttl_seconds=60, max_entries=4)
    first = _search(depart_date=day, fetcher=fetcher, cache=cache)
    second = _search(depart_date=day, fetcher=fetcher, cache=cache)
    assert calls["n"] == 1
    assert first.cached is False
    assert second.cached is True
    assert second.flights[0].price == 108

    _search(depart_date=depart_in(22), fetcher=fetcher, cache=cache)
    assert calls["n"] == 2


def test_timeout_is_a_structured_error(monkeypatch):
    def fetch(query, timeout_seconds: float):
        raise TimeoutError("slow")

    monkeypatch.setattr("flights_mcp.search.fetch_results", fetch)
    with pytest.raises(FlightToolError) as caught:
        run_search(
            origin="ZRH",
            destination="BCN",
            depart_date=depart_in(21),
            timeout_seconds=0.01,
        )
    payload = json.loads(str(caught.value))
    assert payload["code"] == "timeout"
    assert "slow" not in payload["message"]
