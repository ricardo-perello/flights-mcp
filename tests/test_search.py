"""Search behavior that must not touch the network."""

import json
from typing import Any

import pytest

from flights_mcp.errors import FlightToolError
from flights_mcp.google_parse import ParsedItinerary
from flights_mcp.search import _Cache, run_search
from tests.support import depart_in, zurich_barcelona


def _search(**overrides: Any):
    day = overrides.pop("depart_date", depart_in(21))
    fetcher = overrides.pop("fetcher", lambda depart, _return: [zurich_barcelona(depart)])
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
    result = _search(depart_date=day, fetcher=lambda depart, _return: [zurich_barcelona(depart, price=108)])
    assert result.origin == "ZRH"
    assert result.destination == "BCN"
    assert result.currency == "CHF"
    assert result.adults == 1
    assert result.cabin == "economy"
    assert result.cached is False
    assert result.depart_date == day
    flight = result.flights[0]
    assert flight.price == 108
    assert flight.airlines == ["SWISS"]
    assert flight.airline_code == "LX"
    assert flight.stops == 0
    assert flight.route == ["ZRH", "BCN"]
    assert flight.depart_local.endswith("T09:30")
    assert flight.arrive_local.endswith("T11:10")
    assert flight.duration_minutes == 100
    assert flight.duration_basis == "google"
    assert flight.flight_numbers == ["LX1956"]
    assert flight.legs[0].flight_number == "LX1956"
    assert flight.legs[0].flight_number_note is None
    assert flight.layovers == []
    assert flight.carry_on == "unknown"
    assert flight.checked_bag == "unknown"
    assert flight.fare_family is None
    assert "unknown" in flight.fare_note
    assert flight.search_url.startswith("https://www.google.com/travel/flights")
    assert flight.booking_url is not None
    assert flight.booking_url.startswith("https://www.google.com/travel/flights/booking?")
    assert result.picks.cheapest == flight.id
    assert result.picks.fastest == flight.id
    assert result.picks.compromise == flight.id
    assert set(flight.labels) == {"cheapest", "fastest", "compromise"}


def test_connection_keeps_flight_numbers_layover_and_elapsed_time():
    day = depart_in(21)
    result = _search(
        depart_date=day,
        fetcher=lambda depart, _return: [zurich_barcelona(depart, price=77, stops=True)],
    )
    flight = result.flights[0]
    assert flight.price == 77
    assert flight.airlines == ["Air Serbia"]
    assert flight.stops == 1
    assert flight.route == ["ZRH", "BEG", "BCN"]
    assert flight.flight_numbers == ["JU331", "JU580"]
    assert flight.legs[0].origin == "ZRH"
    assert flight.legs[1].destination == "BCN"
    assert flight.duration_basis == "elapsed"
    assert flight.duration_minutes > flight.legs[0].duration_minutes
    assert flight.layovers[0].airport == "BEG"
    assert flight.layovers[0].minutes == 355
    assert flight.layovers[0].minutes_basis == "google"


def test_missing_flight_number_is_explicit():
    day = depart_in(21)

    def fetcher(depart: str, _return: str | None) -> list[ParsedItinerary]:
        item = zurich_barcelona(depart)
        item.legs[0].flight_number = None
        return [item]

    result = _search(
        depart_date=day,
        fetcher=fetcher,
    )
    leg = result.flights[0].legs[0]
    assert leg.flight_number is None
    assert leg.flight_number_note == "Google did not include a flight number for this leg."
    assert result.flights[0].flight_numbers == []


def test_missing_price_is_dropped_and_never_replaced():
    day = depart_in(21)
    result = _search(
        depart_date=day,
        fetcher=lambda depart, _return: [
            zurich_barcelona(depart, price=None),
            zurich_barcelona(depart, price=143),
        ],
    )
    assert [flight.price for flight in result.flights] == [143]
    assert any("without a numeric price" in note for note in result.notes)


def test_only_unpriced_itineraries_are_an_error():
    day = depart_in(21)
    with pytest.raises(FlightToolError) as caught:
        _search(depart_date=day, fetcher=lambda depart, _return: [zurich_barcelona(depart, price=None)])
    payload = json.loads(str(caught.value))
    assert payload["code"] == "unpriced"
    assert "price" not in payload


def test_nonstop_filter_does_not_invent_a_direct_fare():
    day = depart_in(21)
    with pytest.raises(FlightToolError) as caught:
        _search(
            depart_date=day,
            nonstop=True,
            fetcher=lambda depart, _return: [zurich_barcelona(depart, price=77, stops=True)],
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


def test_date_window_labels_cheapest_fastest_and_compromise():
    start = depart_in(21)
    mid = depart_in(22)
    end = depart_in(23)

    def fetcher(day: str, return_date: str | None) -> list[ParsedItinerary]:
        assert return_date is None
        if day == start:
            return [zurich_barcelona(day, price=200, duration_minutes=90, flight_number="LX1952")]
        if day == mid:
            return [zurich_barcelona(day, price=120, duration_minutes=140, flight_number="VY1227")]
        return [zurich_barcelona(day, price=80, duration_minutes=400, flight_number="LX1956")]

    result = _search(depart_date=None, depart_from=start, depart_to=end, fetcher=fetcher)
    by_price = {flight.price: flight for flight in result.flights}
    assert result.depart_date is None
    assert result.dates_searched == [start, mid, end]
    assert result.dates_not_searched == []
    assert result.picks.cheapest == by_price[80].id
    assert result.picks.fastest == by_price[200].id
    assert result.picks.compromise == by_price[120].id
    assert by_price[80].labels == ["cheapest"]
    assert by_price[200].labels == ["fastest"]
    assert by_price[120].labels == ["compromise"]
    assert [flight.price for flight in result.flights] == [80, 120, 200]
    assert [day.cheapest_price for day in result.days] == [200, 120, 80]


def test_long_window_samples_seven_dates_including_the_ends():
    start = depart_in(10)
    end = depart_in(30)
    calls: list[str] = []

    def fetcher(day: str, _return: str | None) -> list[ParsedItinerary]:
        calls.append(day)
        return [zurich_barcelona(day, price=100)]

    result = _search(depart_date=None, depart_from=start, depart_to=end, fetcher=fetcher)
    assert len(calls) == 7
    assert calls[0] == start
    assert calls[-1] == end
    assert result.dates_searched == calls
    assert len(result.dates_not_searched) == 14
    assert start not in result.dates_not_searched
    assert end not in result.dates_not_searched
    assert any("not searched" in note for note in result.notes)


def test_flexibility_searches_neighboring_days():
    day = depart_in(21)
    calls: list[str] = []

    def fetcher(depart: str, _return: str | None) -> list[ParsedItinerary]:
        calls.append(depart)
        return [zurich_barcelona(depart, price=100)]

    result = _search(depart_date=day, flexibility_days=1, fetcher=fetcher)
    assert calls == [depart_in(20), day, depart_in(22)]
    assert result.depart_date == day
    assert result.depart_from == depart_in(20)
    assert result.depart_to == depart_in(22)


def test_nights_sets_the_return_and_conflicts_with_return_date():
    day = depart_in(21)
    seen: list[str | None] = []

    def fetcher(depart: str, return_date: str | None) -> list[ParsedItinerary]:
        seen.append(return_date)
        return [zurich_barcelona(depart, price=220)]

    result = _search(depart_date=day, nights=4, fetcher=fetcher)
    assert seen == [depart_in(25)]
    assert result.nights == 4
    assert result.return_date is None
    assert result.flights[0].return_date == depart_in(25)

    with pytest.raises(FlightToolError) as caught:
        _search(depart_date=day, nights=4, return_date=depart_in(25))
    assert json.loads(str(caught.value))["code"] == "invalid_input"


def test_anchor_and_window_cannot_be_combined():
    with pytest.raises(FlightToolError) as caught:
        _search(depart_date=depart_in(21), depart_from=depart_in(21), depart_to=depart_in(22))
    assert json.loads(str(caught.value))["code"] == "invalid_input"


def test_partial_timeout_keeps_days_already_priced(monkeypatch):
    clock = {"t": 1000.0}

    def monotonic() -> float:
        return clock["t"]

    monkeypatch.setattr("flights_mcp.search.time.monotonic", monotonic)
    start = depart_in(21)
    end = depart_in(23)

    def fetcher(day: str, _return: str | None) -> list[ParsedItinerary]:
        clock["t"] += 0.4
        return [zurich_barcelona(day, price=90)]

    result = run_search(
        origin="ZRH",
        destination="BCN",
        depart_from=start,
        depart_to=end,
        timeout_seconds=1.0,
        fetcher=fetcher,
        cache=None,
    )
    assert result.dates_searched == [start]
    assert result.flights[0].price == 90
    assert end in result.dates_not_searched
    assert any(day.status == "timeout" for day in result.days)


def test_labeled_options_stay_inside_max_results():
    day = depart_in(21)

    def fetcher(depart: str, _return: str | None) -> list[ParsedItinerary]:
        return [
            zurich_barcelona(depart, price=50, duration_minutes=400, flight_number="LX1956"),
            zurich_barcelona(depart, price=80, duration_minutes=200, flight_number="LX1952"),
            zurich_barcelona(depart, price=200, duration_minutes=90, flight_number="LX1954"),
            zurich_barcelona(depart, price=90, duration_minutes=300, flight_number="LX1950"),
        ]

    result = _search(depart_date=day, max_results=2, fetcher=fetcher)
    ids = {flight.id for flight in result.flights}
    assert result.count == 2
    assert result.picks.cheapest in ids
    assert result.picks.fastest in ids
    assert result.picks.compromise in ids
    assert any(flight.price == 200 for flight in result.flights)


def test_identical_searches_reuse_the_process_cache():
    day = depart_in(21)
    calls = {"n": 0}

    def fetcher(depart: str, _return: str | None):
        calls["n"] += 1
        return [zurich_barcelona(depart, price=108)]

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
