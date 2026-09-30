"""Parser behavior on a synthetic Google payload. No network."""

import json

import pytest
from fast_flights.exceptions import FlightsNotFound

from flights_mcp.google_parse import parse_html


def _segment(
    origin: str,
    destination: str,
    flight: list[object],
    *,
    depart_clock: list[object],
    arrive_clock: list[object],
    air_minutes: int = 100,
) -> list[object]:
    raw: list[object] = [None] * 23
    raw[3] = origin
    raw[4] = f"{origin} Airport"
    raw[5] = f"{destination} Airport"
    raw[6] = destination
    raw[8] = depart_clock
    raw[10] = arrive_clock
    raw[11] = air_minutes
    raw[17] = "Airbus A320"
    raw[20] = [2026, 10, 28]
    raw[21] = [2026, 10, 28]
    raw[22] = flight
    return raw


def _item(
    *,
    code: str,
    name: str,
    segments: list[list[object]],
    price: int | None,
    duration: int,
    layovers: list[object] | None,
    token: str | None = "CjRIabcdefgh",
) -> list[object]:
    flight: list[object] = [None] * 23
    flight[0] = code
    flight[1] = [name]
    flight[2] = segments
    flight[9] = duration
    flight[13] = layovers
    flight[22] = [None, None, None, None, None, None, None, 76000]
    price_block: list[object] = [[None, price] if price is not None else [None, None]]
    if token is not None:
        price_block.append(token)
    return [flight, price_block]


def _page(payload: object) -> str:
    return (
        '<html><script class="ds:1">AF_initDataCallback({data:'
        + json.dumps(payload)
        + ",});</script></html>"
    )


def test_parser_keeps_flight_numbers_layovers_and_both_groups():
    highlighted = _item(
        code="VY",
        name="Vueling",
        segments=[_segment("ZRH", "BCN", ["VY", 1227, None, "Vueling"], depart_clock=[8], arrive_clock=[9, 40])],
        price=57,
        duration=100,
        layovers=None,
    )
    listed = _item(
        code="JU",
        name="Air Serbia",
        segments=[
            _segment(
                "ZRH",
                "BEG",
                ["JU", "331", None, "Air Serbia"],
                depart_clock=[9, 30],
                arrive_clock=[11, 15],
                air_minutes=105,
            ),
            _segment(
                "BEG",
                "BCN",
                ["JU", 580, None, "Air Serbia"],
                depart_clock=[None, 31],
                arrive_clock=[2, 10],
                air_minutes=99,
            ),
        ],
        price=77,
        duration=1420,
        layovers=[[1155, "BEG", "BEG", None, "Belgrade Nikola Tesla Airport", "Belgrade"]],
    )
    # A legroom-sized integer in an unused slot must not become a bag or a price.
    listed[0][14] = 30
    payload = [None, None, [[highlighted]], [[listed]]]

    found = parse_html(_page(payload))
    assert [item.price for item in found] == [57, 77]
    vueling, serbia = found
    assert vueling.legs[0].flight_number == "VY1227"
    assert vueling.legs[0].depart_local == "2026-10-28T08:00"
    assert vueling.layovers == []
    assert serbia.legs[0].flight_number == "JU331"
    assert serbia.legs[1].flight_number == "JU580"
    assert serbia.legs[1].depart_local == "2026-10-28T00:31"
    assert serbia.layovers[0].airport == "BEG"
    assert serbia.layovers[0].minutes == 1155
    assert serbia.booking_token == "CjRIabcdefgh"
    assert not hasattr(vueling, "carry_on")


def test_missing_price_and_flight_number_stay_absent():
    item = _item(
        code="LX",
        name="SWISS",
        segments=[_segment("ZRH", "BCN", [None, None], depart_clock=[7, 20], arrive_clock=[9, 5])],
        price=None,
        duration=105,
        layovers=None,
        token=None,
    )
    found = parse_html(_page([None, None, [], [[item]]]))
    assert len(found) == 1
    assert found[0].price is None
    assert found[0].legs[0].flight_number is None
    assert found[0].booking_token is None


def test_error_page_and_missing_script_are_not_results():
    with pytest.raises(FlightsNotFound):
        parse_html("<html><p>no script</p></html>")
    with pytest.raises(FlightsNotFound):
        parse_html('<script class="ds:1">AF_initDataCallback({data:null,errorHasStatus: true,});</script>')
