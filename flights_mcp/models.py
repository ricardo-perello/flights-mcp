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
DurationBasis = Literal["elapsed", "air"]

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
    aircraft: str | None = Field(description="Aircraft type when the source provides one.")


class Itinerary(StrictModel):
    price: int = Field(
        ge=1,
        le=10_000_000,
        description=(
            "Total fare in major units of currency, as Google returned it for "
            "this search. Not converted, rounded, or estimated."
        ),
    )
    currency: str = Field(min_length=3, max_length=3)
    airlines: list[str] = Field(
        description="Airline names. This source does not provide flight numbers.",
        max_length=6,
    )
    airline_code: str | None = Field(
        description="Two-character airline code when the source names a single airline.",
        min_length=2,
        max_length=2,
    )
    depart_local: str = Field(description="Local departure of the first leg.", max_length=16)
    arrive_local: str = Field(description="Local arrival of the last leg.", max_length=16)
    duration_minutes: int = Field(ge=1, le=60 * 48)
    duration_basis: DurationBasis = Field(
        description=(
            "elapsed is door-to-door using airport timezones. "
            "air is the sum of airborne minutes when a timezone is missing."
        ),
    )
    duration: str = Field(description="Same duration, written like 1h 40m.", max_length=20)
    stops: int = Field(ge=0, le=5, description="Number of stops. Zero is nonstop.")
    route: list[str] = Field(description="Airport IATA codes in order, including the origin.", max_length=8)
    legs: list[FlightLeg] = Field(min_length=1, max_length=6)
    search_url: str = Field(description="Google Flights search for this query.", max_length=2000)
    carbon_emission_grams: int | None = Field(
        description="Trip carbon estimate in grams, when Google provides one.",
        ge=0,
    )


class SearchFlightsResult(StrictModel):
    origin: str = Field(description="Resolved origin IATA or metro code.", min_length=3, max_length=3)
    origin_name: str = Field(max_length=160)
    destination: str = Field(description="Resolved destination IATA or metro code.", min_length=3, max_length=3)
    destination_name: str = Field(max_length=160)
    depart_date: str
    return_date: str | None = Field(description="Null for a one-way search.")
    adults: int = Field(ge=1, le=MAX_ADULTS)
    cabin: Cabin
    currency: str = Field(min_length=3, max_length=3)
    nonstop: bool
    count: int = Field(ge=1, le=MAX_RESULTS)
    cached: bool = Field(description="True when this answer was reused from the short in-process cache.")
    source: Literal["google_flights"]
    search_url: str = Field(max_length=2000)
    flights: list[Itinerary] = Field(min_length=1, max_length=MAX_RESULTS)
    notes: list[str] = Field(max_length=8)
