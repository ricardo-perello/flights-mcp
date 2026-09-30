"""Strict input and output models.

These types are the tool schemas. The SDK publishes them on tools/list and
validates both arguments and return values against them.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime, timedelta
from typing import Annotated, Literal, get_args
from zoneinfo import ZoneInfo

from fast_flights.types import Currency
from pydantic import AfterValidator, BaseModel, ConfigDict, Field

ZURICH = ZoneInfo("Europe/Zurich")
DATE_HORIZON_DAYS = 330
MAX_RESULTS = 20
DEFAULT_MAX_RESULTS = 8
MAX_ADULTS = 9
MAX_PLACE_LENGTH = 80
RESOLVE_LIMIT_DEFAULT = 10
RESOLVE_LIMIT_MAX = 20

CABINS = ("economy", "premium-economy", "business", "first")
Cabin = Literal["economy", "premium-economy", "business", "first"]
PlaceKind = Literal["airport", "metro"]
DurationBasis = Literal["google", "elapsed", "air"]
OptionLabel = Literal["cheapest", "fastest", "compromise"]
BagStatus = Literal["included", "excluded", "unknown"]
DayStatus = Literal["ok", "no_results", "unpriced", "timeout", "upstream", "skipped"]
LayoverBasis = Literal["google", "schedule", "unknown"]

ALLOWED_CURRENCIES = frozenset(get_args(Currency))

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


def zurich_today() -> date:
    return datetime.now(ZURICH).date()


def clean_place(value: str) -> str:
    """Collapse whitespace and reject control characters. Keep city spelling."""
    if _CONTROL_RE.search(value):
        raise ValueError("must not contain control characters")
    cleaned = " ".join(value.split())
    if not cleaned:
        raise ValueError("must not be empty")
    if len(cleaned) > MAX_PLACE_LENGTH:
        raise ValueError(f"must be at most {MAX_PLACE_LENGTH} characters")
    return cleaned


def normalize_name(value: str) -> str:
    """Case- and accent-insensitive key for city and airport names."""
    decomposed = unicodedata.normalize("NFKD", value)
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    lowered = stripped.lower()
    chars = [ch if ch.isalnum() else " " for ch in lowered]
    return " ".join("".join(chars).split())


def parse_flight_date(value: str) -> str:
    if not _DATE_RE.fullmatch(value):
        raise ValueError("must be YYYY-MM-DD")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("must be a real calendar date") from exc
    today = zurich_today()
    if parsed < today:
        raise ValueError(f"must be on or after {today.isoformat()} (Europe/Zurich)")
    horizon = today + timedelta(days=DATE_HORIZON_DAYS)
    if parsed > horizon:
        raise ValueError(f"must be on or before {horizon.isoformat()} (Europe/Zurich)")
    return value


def parse_currency(value: str) -> str:
    code = value.strip().upper()
    if code not in ALLOWED_CURRENCIES:
        raise ValueError(
            f"unsupported currency {code!r}. Examples: CHF, EUR, USD, GBP."
        )
    return code


PlaceName = Annotated[
    str,
    Field(
        min_length=1,
        max_length=MAX_PLACE_LENGTH,
        description=(
            "IATA airport or city code (ZRH, BCN, NYC) or a city or airport "
            "name (Zurich, Barcelona)."
        ),
    ),
    AfterValidator(clean_place),
]

FlightDate = Annotated[
    str,
    Field(
        description="Calendar date YYYY-MM-DD, interpreted in Europe/Zurich.",
        min_length=10,
        max_length=10,
        pattern=r"^\d{4}-\d{2}-\d{2}$",
    ),
    AfterValidator(parse_flight_date),
]

CurrencyCode = Annotated[
    str,
    Field(
        description="ISO 4217 currency Google Flights can price in. Default CHF.",
        min_length=3,
        max_length=3,
        pattern=r"^[A-Za-z]{3}$",
    ),
    AfterValidator(parse_currency),
]


class AirportMatch(StrictModel):
    """One airport or a multi-airport city code."""

    iata: str = Field(description="IATA airport code, or a metro code such as NYC.", min_length=3, max_length=3)
    name: str = Field(description="Airport or metro name.", max_length=160)
    city: str = Field(description="City name. Empty when the source has none.", max_length=80)
    country: str = Field(description="ISO 3166-1 alpha-2 country code when known.", max_length=2)
    timezone: str = Field(
        description="IANA timezone of the airport. Empty for a metro that spans timezones.",
        max_length=64,
    )
    kind: PlaceKind = Field(description="airport is one field. metro is a city code covering several.")


class ResolveAirportsResult(StrictModel):
    query: str = Field(description="The query after whitespace was collapsed.", max_length=MAX_PLACE_LENGTH)
    count: int = Field(ge=1, description="Number of matches returned.")
    airports: list[AirportMatch]


class FlightLeg(StrictModel):
    origin: str = Field(description="Departure airport IATA.", min_length=3, max_length=3)
    origin_name: str = Field(max_length=160)
    destination: str = Field(description="Arrival airport IATA.", min_length=3, max_length=3)
    destination_name: str = Field(max_length=160)
    depart_local: str = Field(description="Local departure, YYYY-MM-DDTHH:MM, no offset.", max_length=16)
    arrive_local: str = Field(description="Local arrival, YYYY-MM-DDTHH:MM, no offset.", max_length=16)
    duration_minutes: int = Field(ge=0, le=60 * 24, description="Air time of this leg, in minutes.")
    aircraft: str | None = Field(description="Aircraft type when Google provides one. Null when it does not.")
    airline: str | None = Field(description="Airline name on this leg, when Google provides one.")
    airline_code: str | None = Field(description="Two-character airline code, when present.", min_length=2, max_length=2)
    flight_number: str | None = Field(
        description="Flight number such as LX1956. Null when Google did not send one for this leg.",
        max_length=8,
    )
    flight_number_note: str | None = Field(
        description="Set when flight_number is null, so the absence is explicit. Null when the number is present.",
    )
    codeshare_flight_number: str | None = Field(
        description="Marketing or codeshare number when Google sent a second identifier. Null when it did not.",
        max_length=8,
    )


class Layover(StrictModel):
    airport: str = Field(min_length=3, max_length=3)
    airport_name: str = Field(max_length=160)
    city: str | None = Field(description="City name when Google included it.")
    minutes: int | None = Field(description="Layover length. Null only when it could not be read or computed.", ge=0, le=60 * 72)
    minutes_basis: LayoverBasis = Field(
        description="google is the layover Google reported. schedule is the gap between the leg times Google sent.",
    )


class Itinerary(StrictModel):
    id: str = Field(description="Stable id for this option inside the response.", max_length=80)
    labels: list[OptionLabel] = Field(
        description="cheapest, fastest, and compromise. One option can carry more than one label.",
        max_length=3,
    )
    depart_date: str
    return_date: str | None
    price: int = Field(
        ge=1,
        le=10_000_000,
        description="Total fare in major currency units, as Google returned it. Not converted or estimated.",
    )
    currency: str = Field(min_length=3, max_length=3)
    airlines: list[str] = Field(max_length=6)
    airline_code: str | None = Field(min_length=2, max_length=2)
    flight_numbers: list[str] = Field(
        description="Flight numbers Google sent, in leg order. Empty when none of the legs had one.",
        max_length=6,
    )
    depart_local: str = Field(max_length=16)
    arrive_local: str = Field(max_length=16)
    duration_minutes: int = Field(ge=1, le=60 * 72)
    duration_basis: DurationBasis = Field(
        description="google is the trip duration from the payload. elapsed uses airport timezones. air is airborne time only.",
    )
    duration: str = Field(max_length=20)
    stops: int = Field(ge=0, le=5)
    route: list[str] = Field(max_length=8)
    legs: list[FlightLeg] = Field(min_length=1, max_length=6)
    layovers: list[Layover] = Field(
        description="One entry per stop. Empty when the itinerary is nonstop.",
        max_length=5,
    )
    carry_on: BagStatus = Field(description="included, excluded, or unknown. unknown means the payload did not say.")
    checked_bag: BagStatus
    fare_family: str | None = Field(description="Fare brand such as Basic or Standard. Null when Google did not name one.")
    fare_note: str = Field(description="Explicit statement of what is known about bags and fare family.")
    search_url: str = Field(description="Google Flights search for this date.", max_length=2000)
    booking_url: str | None = Field(
        description="Google Flights link for this itinerary when a booking token was present. Not a confirmed ticket.",
        max_length=2000,
    )
    carbon_emission_grams: int | None = Field(ge=0)


class DayQuote(StrictModel):
    date: str
    return_date: str | None
    status: DayStatus
    cheapest_price: int | None = Field(description="Lowest priced itinerary that day. Null when the day produced none.")
    currency: str = Field(min_length=3, max_length=3)
    itineraries: int = Field(ge=0)
    detail: str | None = Field(description="Why a day has no fare, when status is not ok.")


class Picks(StrictModel):
    cheapest: str = Field(description="Itinerary id with the lowest price.")
    fastest: str = Field(description="Itinerary id with the shortest duration.")
    compromise: str = Field(description="Itinerary id balancing price and duration. May match another pick when little else exists.")


class SearchFlightsResult(StrictModel):
    origin: str = Field(min_length=3, max_length=3)
    origin_name: str = Field(max_length=160)
    destination: str = Field(min_length=3, max_length=3)
    destination_name: str = Field(max_length=160)
    depart_date: str | None = Field(
        description="The depart_date argument when the search was anchored on one day, with or without flexibility. Null when the search used depart_from and depart_to.",
    )
    depart_from: str
    depart_to: str
    dates_searched: list[str] = Field(max_length=7)
    dates_not_searched: list[str] = Field(
        description="Dates inside the requested window that were not queried, because of the 7-date cap or the timeout.",
        max_length=366,
    )
    return_date: str | None = Field(description="Fixed return date, when one was requested. Null for one-way or when nights varies the return.")
    nights: int | None = Field(description="Length of stay applied to each departure, when requested.")
    adults: int = Field(ge=1, le=MAX_ADULTS)
    cabin: Cabin
    currency: str = Field(min_length=3, max_length=3)
    nonstop: bool
    count: int = Field(ge=1, le=MAX_RESULTS)
    cached: bool
    source: Literal["google_flights"]
    search_url: str = Field(description="Google Flights search for the first date that was actually queried.", max_length=2000)
    days: list[DayQuote] = Field(max_length=7)
    picks: Picks
    flights: list[Itinerary] = Field(min_length=1, max_length=MAX_RESULTS)
    notes: list[str] = Field(max_length=12)
