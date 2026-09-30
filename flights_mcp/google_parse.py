"""Read itineraries out of the Google Flights page payload.

fast-flights builds the query. This parser keeps the fields a planner needs
when they are actually present: flight number, layover, price, times.
Indexes were checked against a live ZRH→BCN payload (October 2026). A slot
that does not match that shape is left empty. Nothing is inferred.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from selectolax.lexbor import LexborHTMLParser

from fast_flights.exceptions import FlightsNotFound


@dataclass
class ParsedLeg:
    origin: str
    origin_name: str
    destination: str
    destination_name: str
    depart_local: str
    arrive_local: str
    duration_minutes: int
    aircraft: str | None
    flight_number: str | None
    airline_code: str | None
    airline_name: str | None
    codeshare_flight_number: str | None


@dataclass
class ParsedLayover:
    airport: str
    airport_name: str
    city: str | None
    minutes: int | None


@dataclass
class ParsedItinerary:
    price: int | None
    airlines: list[str]
    airline_code: str | None
    legs: list[ParsedLeg]
    layovers: list[ParsedLayover]
    duration_minutes: int | None
    carbon_emission_grams: int | None
    booking_token: str | None


def _text(value: object, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = " ".join(value.split())
    if not cleaned:
        return None
    return cleaned[:limit]


def _clock(value: object) -> tuple[int, int] | None:
    """Google omits zero components: [8] is 08:00 and [None, 31] is 00:31."""
    if value is None:
        return (0, 0)
    if not isinstance(value, list):
        return None
    padded = [*(value[:2]), None, None]
    hour = padded[0] or 0
    minute = padded[1] or 0
    if isinstance(hour, bool) or isinstance(minute, bool):
        return None
    if not isinstance(hour, int) or not isinstance(minute, int):
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour, minute


def _ymd(value: object) -> tuple[int, int, int] | None:
    if not isinstance(value, list) or len(value) < 3:
        return None
    year, month, day = value[0], value[1], value[2]
    if any(isinstance(part, bool) or not isinstance(part, int) for part in (year, month, day)):
        return None
    if not (2000 <= year <= 2100 and 1 <= month <= 12 and 1 <= day <= 31):
        return None
    return year, month, day


def _stamp(date_value: object, time_value: object) -> str | None:
    ymd = _ymd(date_value)
    clock = _clock(time_value)
    if ymd is None or clock is None:
        return None
    year, month, day = ymd
    hour, minute = clock
    return f"{year:04d}-{month:02d}-{day:02d}T{hour:02d}:{minute:02d}"


def _flight_ident(value: object) -> tuple[str, str, str | None] | None:
    """Return (flight_number, airline_code, airline_name) or nothing."""
    if not isinstance(value, list) or len(value) < 2:
        return None
    code = _text(value[0], 3)
    number = value[1]
    if code is None or len(code) != 2 or not code.isalnum():
        return None
    if isinstance(number, bool) or not isinstance(number, (str, int)):
        return None
    digits = str(number).strip()
    if not digits.isdigit() or not 1 <= len(digits) <= 4:
        return None
    name = _text(value[3], 60) if len(value) > 3 else None
    return f"{code.upper()}{digits}", code.upper(), name


def _price(value: object) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int) and 0 < value <= 10_000_000:
        return value
    if isinstance(value, float) and value.is_integer():
        whole = int(value)
        return whole if 0 < whole <= 10_000_000 else None
    if isinstance(value, str) and value.isascii() and value.isdigit():
        whole = int(value)
        return whole if 0 < whole <= 10_000_000 else None
    return None


def _leg(raw: object) -> ParsedLeg | None:
    if not isinstance(raw, list) or len(raw) < 22:
        return None
    origin = _text(raw[3], 3)
    destination = _text(raw[6], 3)
    if origin is None or destination is None or len(origin) != 3 or len(destination) != 3:
        return None
    depart_local = _stamp(raw[20], raw[8])
    arrive_local = _stamp(raw[21], raw[10])
    duration = raw[11]
    if depart_local is None or arrive_local is None:
        return None
    if isinstance(duration, bool) or not isinstance(duration, int) or not 0 <= duration <= 60 * 24:
        return None
    ident = _flight_ident(raw[22]) if len(raw) > 22 else None
    codeshare = None
    if len(raw) > 15 and isinstance(raw[15], list) and raw[15]:
        shared = _flight_ident(raw[15][0])
        if shared is not None:
            codeshare = shared[0]
    aircraft = _text(raw[17], 80) if len(raw) > 17 else None
    return ParsedLeg(
        origin=origin.upper(),
        origin_name=_text(raw[4], 160) or origin.upper(),
        destination=destination.upper(),
        destination_name=_text(raw[5], 160) or destination.upper(),
        depart_local=depart_local,
        arrive_local=arrive_local,
        duration_minutes=duration,
        aircraft=aircraft,
        flight_number=ident[0] if ident else None,
        airline_code=ident[1] if ident else None,
        airline_name=ident[2] if ident else None,
        codeshare_flight_number=codeshare,
    )


def _layovers(raw: object) -> list[ParsedLayover]:
    if not isinstance(raw, list):
        return []
    found: list[ParsedLayover] = []
    for entry in raw:
        if not isinstance(entry, list) or len(entry) < 2:
            continue
        minutes = entry[0] if isinstance(entry[0], int) and not isinstance(entry[0], bool) else None
        if minutes is not None and not 0 <= minutes <= 60 * 72:
            minutes = None
        airport = _text(entry[1], 3)
        if airport is None or len(airport) != 3:
            continue
        name = _text(entry[4], 160) if len(entry) > 4 else None
        city = _text(entry[5], 80) if len(entry) > 5 else None
        found.append(
            ParsedLayover(
                airport=airport.upper(),
                airport_name=name or airport.upper(),
                city=city,
                minutes=minutes,
            )
        )
    return found


def _carbon(flight: list[object]) -> int | None:
    if len(flight) <= 22 or not isinstance(flight[22], list) or len(flight[22]) <= 7:
        return None
    grams = flight[22][7]
    if isinstance(grams, bool) or not isinstance(grams, int) or grams < 0 or grams > 5_000_000:
        return None
    return grams


def _itinerary(item: object) -> ParsedItinerary | None:
    if not isinstance(item, list) or not item or not isinstance(item[0], list):
        return None
    flight = item[0]
    if len(flight) < 10 or not isinstance(flight[2], list):
        return None
    legs: list[ParsedLeg] = []
    for raw in flight[2][:6]:
        leg = _leg(raw)
        if leg is None:
            return None
        legs.append(leg)
    if not legs:
        return None
    price = None
    token = None
    if len(item) > 1 and isinstance(item[1], list):
        block = item[1]
        if block and isinstance(block[0], list) and len(block[0]) > 1:
            price = _price(block[0][1])
        if len(block) > 1 and isinstance(block[1], str) and 8 <= len(block[1]) <= 1500:
            token = block[1]
    airlines: list[str] = []
    if isinstance(flight[1], list):
        for name in flight[1]:
            cleaned = _text(name, 60)
            if cleaned:
                airlines.append(cleaned)
            if len(airlines) == 6:
                break
    code = _text(flight[0], 8)
    airline_code = code.upper() if code and len(code) == 2 and code.isalnum() else None
    duration = flight[9] if isinstance(flight[9], int) and not isinstance(flight[9], bool) else None
    if duration is not None and not 1 <= duration <= 60 * 72:
        duration = None
    return ParsedItinerary(
        price=price,
        airlines=airlines,
        airline_code=airline_code,
        legs=legs,
        layovers=_layovers(flight[13]) if len(flight) > 13 else [],
        duration_minutes=duration,
        carbon_emission_grams=_carbon(flight),
        booking_token=token,
    )


def iter_itineraries(payload: object) -> list[ParsedItinerary]:
    """Both result groups: the highlighted fares and the rest of the list."""
    if not isinstance(payload, list):
        return []
    found: list[ParsedItinerary] = []
    for index in (2, 3):
        if index >= len(payload) or not isinstance(payload[index], list):
            continue
        for section in payload[index]:
            if not isinstance(section, list) or not section:
                continue
            first = section[0]
            if not isinstance(first, list) or not first or not isinstance(first[0], list):
                continue
            for item in section:
                parsed = _itinerary(item)
                if parsed is not None:
                    found.append(parsed)
    return found


def parse_html(html: str) -> list[ParsedItinerary]:
    script = LexborHTMLParser(html).css_first(r"script.ds\:1")
    if script is None or not script.text():
        raise FlightsNotFound("Google Flights returned a page without flight data")
    raw = script.text()
    if "data:" not in raw:
        raise FlightsNotFound("Google Flights returned a page without flight data")
    data = raw.split("data:", 1)[1].rsplit(",", 1)[0]
    if data.endswith("errorHasStatus: true"):
        raise FlightsNotFound("no flights found; received error")
    payload = json.loads(data)
    return iter_itineraries(payload)
