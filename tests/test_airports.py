"""Airport resolution stays on the local airportsdata snapshot."""

import pytest

from flights_mcp.airports import PlaceError, pick_place, resolve_airports


def test_zurich_and_barcelona_names_and_codes():
    zurich = pick_place("Zurich")
    assert zurich.iata == "ZRH"
    assert zurich.kind == "airport"
    assert zurich.country == "CH"
    assert "Zurich" in zurich.name or "Zürich" in zurich.name

    accented = pick_place("Zürich")
    assert accented.iata == "ZRH"

    assert pick_place("zrh").iata == "ZRH"

    barcelona = pick_place("Barcelona")
    assert barcelona.iata == "BCN"
    assert barcelona.country == "ES"
    assert pick_place("BCN").iata == "BCN"


def test_resolve_lists_zurich_and_barcelona():
    zurich = resolve_airports("Zurich", limit=5)
    assert zurich[0].iata == "ZRH"
    barcelona = resolve_airports("Barcelona", limit=5)
    assert barcelona[0].iata == "BCN"


def test_new_york_metro_includes_the_main_airports():
    matches = resolve_airports("NYC", limit=10)
    codes = [match.iata for match in matches]
    assert codes[0] == "NYC"
    # This airportsdata snapshot's NYC metro is Kennedy and LaGuardia.
    # Newark (EWR) is its own airport, city Newark.
    assert {"JFK", "LGA"} <= set(codes)
    picked = pick_place("New York")
    assert picked.kind == "metro"
    assert picked.iata == "NYC"


def test_unknown_and_ambiguous_places():
    with pytest.raises(PlaceError) as unknown:
        pick_place("ZZZ")
    assert unknown.value.code == "unknown_place"

    with pytest.raises(PlaceError) as missing:
        pick_place("Not A Real City Anywhere")
    assert missing.value.code == "unknown_place"

    with pytest.raises(PlaceError) as ambiguous:
        pick_place("Springfield")
    assert ambiguous.value.code == "ambiguous_place"
    assert len(ambiguous.value.candidates) >= 2


def test_resolve_respects_limit_and_empty_query():
    matches = resolve_airports("London", limit=3)
    assert len(matches) == 3
    assert resolve_airports("", limit=5) == []
    assert resolve_airports("   ", limit=5) == []
