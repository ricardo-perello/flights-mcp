"""One real Google Flights search.

Skipped unless FLIGHTS_LIVE=1. This is the only test that uses the network.
It records the priced itineraries to /tmp/flights-live.json for the README note.
"""

import json
import os

import pytest

from flights_mcp.search import run_search
from tests.support import depart_in

pytestmark = pytest.mark.skipif(
    os.environ.get("FLIGHTS_LIVE") != "1",
    reason="set FLIGHTS_LIVE=1 to search live public fares",
)


def test_live_zurich_to_barcelona_week():
    """One-way ZRH to BCN across seven days, a few weeks out, 1 adult, economy, CHF."""
    start = depart_in(26)
    end = depart_in(32)
    read_fd, write_fd = os.pipe()
    saved_stdout = os.dup(1)
    os.dup2(write_fd, 1)
    try:
        result = run_search(
            origin="ZRH",
            destination="BCN",
            depart_from=start,
            depart_to=end,
            adults=1,
            cabin="economy",
            currency="CHF",
            max_results=8,
            nonstop=False,
            cache=None,
        )
    finally:
        os.dup2(saved_stdout, 1)
        os.close(saved_stdout)
        os.close(write_fd)
    leaked = os.read(read_fd, 1_000_000)
    os.close(read_fd)
    assert leaked == b"", "live search wrote to stdout; that would break stdio MCP"
    dumped = result.model_dump(mode="json")
    with open("/tmp/flights-live.json", "w", encoding="utf-8") as handle:
        json.dump(dumped, handle, indent=2)
        handle.write("\n")

    assert result.origin == "ZRH"
    assert result.destination == "BCN"
    assert result.currency == "CHF"
    assert 1 <= result.count <= 8
    assert result.search_url.startswith("https://www.google.com/travel/flights")
    assert result.dates_searched
    assert result.dates_searched[0] >= start
    assert result.dates_searched[-1] <= end
    ids = {flight.id for flight in result.flights}
    assert result.picks.cheapest in ids
    assert result.picks.fastest in ids
    assert result.picks.compromise in ids
    assert any(flight.flight_numbers for flight in result.flights)
    for flight in result.flights:
        assert flight.price > 0
        assert flight.airlines
        assert flight.depart_local
        assert flight.arrive_local
        assert flight.stops >= 0
        assert flight.route[0] == "ZRH"
        assert flight.route[-1] == "BCN"
        assert flight.legs
        assert flight.carry_on == "unknown"
        assert flight.checked_bag == "unknown"
        assert flight.fare_family is None
        assert "unknown" in flight.fare_note
        for leg in flight.legs:
            if leg.flight_number is None:
                assert leg.flight_number_note
        if flight.stops:
            assert flight.layovers
            assert all(stop.airport for stop in flight.layovers)
