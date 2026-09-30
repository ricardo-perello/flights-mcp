"""Live search: resolve airports, call Google Flights, keep only priced trips."""

from __future__ import annotations

import logging
import time
from collections import OrderedDict
from datetime import datetime
from threading import Lock
from typing import Any, Callable
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from fast_flights import FlightQuery, FlightsNotFound, Passengers, create_query
from fast_flights.querying import Query

from flights_mcp.airports import PlaceError, pick_place, place_error_to_tool, timezone_for
from flights_mcp.errors import FlightToolError
from flights_mcp.models import (
    DEFAULT_MAX_RESULTS,
    MAX_ADULTS,
    MAX_RESULTS,
    Cabin,
    FlightLeg,
    Itinerary,
    SearchFlightsResult,
)
from flights_mcp.upstream import UpstreamFailure, fetch_results

log = logging.getLogger("flights_mcp")

SEARCH_TIMEOUT_SECONDS = 25.0
CACHE_TTL_SECONDS = 120.0
CACHE_MAX_ENTRIES = 32

_NOTES = (
    "Fares are a live public snapshot and can change before you book.",
    "Flight numbers, baggage allowances, and fare rules are not in this source.",
    "Times are local to each airport and have no timezone offset.",
)

Fetcher = Callable[..., list[Any]]
_UNSET = object()


class _Cache:
    def __init__(self, ttl_seconds: float, max_entries: int) -> None:
        self._ttl = ttl_seconds
        self._max = max_entries
        self._lock = Lock()
        self._items: OrderedDict[tuple[Any, ...], tuple[float, SearchFlightsResult]] = OrderedDict()

    def get(self, key: tuple[Any, ...]) -> SearchFlightsResult | None:
        now = time.monotonic()
        with self._lock:
            found = self._items.get(key)
            if found is None:
                return None
            stored_at, value = found
            if now - stored_at > self._ttl:
                self._items.pop(key, None)
                return None
            self._items.move_to_end(key)
            return value.model_copy(update={"cached": True})

    def put(self, key: tuple[Any, ...], value: SearchFlightsResult) -> None:
        with self._lock:
            self._items[key] = (time.monotonic(), value)
            self._items.move_to_end(key)
            while len(self._items) > self._max:
                self._items.popitem(last=False)


_CACHE = _Cache(CACHE_TTL_SECONDS, CACHE_MAX_ENTRIES)


def _minutes_label(minutes: int) -> str:
    hours, mins = divmod(int(minutes), 60)
    if hours and mins:
        return f"{hours}h {mins}m"
    if hours:
        return f"{hours}h"
    return f"{mins}m"


def _local_stamp(value: Any) -> str | None:
    try:
        year, month, day = value.date
        hour, minute = value.time
        return f"{int(year):04d}-{int(month):02d}-{int(day):02d}T{int(hour):02d}:{int(minute):02d}"
    except (TypeError, ValueError):
        return None


def _aware(stamp: str, tz_name: str | None) -> datetime | None:
    if not tz_name:
        return None
    try:
        return datetime.fromisoformat(stamp).replace(tzinfo=ZoneInfo(tz_name))
    except (ValueError, KeyError, OSError):
        return None


def _price(value: Any) -> int | None:
    """Accept only a positive whole number the source already expressed. Never round."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value if 0 < value <= 10_000_000 else None
    if isinstance(value, float) and value.is_integer():
        whole = int(value)
        return whole if 0 < whole <= 10_000_000 else None
    if isinstance(value, str) and value.isascii() and value.isdigit():
        whole = int(value)
        return whole if 0 < whole <= 10_000_000 else None
    return None


def _airline_code(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    code = value.strip().upper()
    if len(code) == 2 and code.isalnum() and code != "MULTI":
        return code
    return None


def _search_url(query: Query, *, origin: str, destination: str, depart_date: str, return_date: str | None, currency: str) -> str:
    try:
        url = query.url()
    except Exception:
        url = ""
    if isinstance(url, str) and url.startswith("https://www.google.com/travel/flights") and len(url) <= 2000:
        return url
    text = f"Flights to {destination} from {origin} on {depart_date}"
    if return_date:
        text = f"{text} through {return_date}"
    return "https://www.google.com/travel/flights?" + urlencode({"q": text, "curr": currency, "hl": "en"})


def _leg(raw: Any) -> FlightLeg | None:
    try:
        origin = str(raw.from_airport.code).strip().upper()
        destination = str(raw.to_airport.code).strip().upper()
        origin_name = str(raw.from_airport.name or origin).strip()[:160]
        destination_name = str(raw.to_airport.name or destination).strip()[:160]
        depart_local = _local_stamp(raw.departure)
        arrive_local = _local_stamp(raw.arrival)
        duration = raw.duration
    except AttributeError:
        return None
    if len(origin) != 3 or len(destination) != 3 or not depart_local or not arrive_local:
        return None
    if isinstance(duration, bool) or not isinstance(duration, int) or duration < 0 or duration > 60 * 24:
        return None
    aircraft_raw = getattr(raw, "plane_type", None)
    aircraft = str(aircraft_raw).strip()[:80] if isinstance(aircraft_raw, str) and aircraft_raw.strip() else None
    return FlightLeg(
        origin=origin,
        origin_name=origin_name or origin,
        destination=destination,
        destination_name=destination_name or destination,
        depart_local=depart_local,
        arrive_local=arrive_local,
        duration_minutes=duration,
        aircraft=aircraft,
    )


def _itinerary(raw: Any, *, currency: str, search_url: str) -> Itinerary | None:
    price = _price(getattr(raw, "price", None))
    if price is None:
        return None
    raw_legs = getattr(raw, "flights", None) or []
    if len(raw_legs) < 1 or len(raw_legs) > 6:
        return None
    legs: list[FlightLeg] = []
    for item in raw_legs:
        leg = _leg(item)
        if leg is None:
            return None
        legs.append(leg)

    airlines_raw = getattr(raw, "airlines", None) or []
    airlines: list[str] = []
    for name in airlines_raw:
        if isinstance(name, str):
            cleaned = " ".join(name.split())
            if cleaned:
                airlines.append(cleaned[:60])
        if len(airlines) == 6:
            break

    route = [legs[0].origin]
    for leg in legs:
        route.append(leg.destination)

    air_minutes = sum(leg.duration_minutes for leg in legs)
    start = _aware(legs[0].depart_local, timezone_for(legs[0].origin))
    end = _aware(legs[-1].arrive_local, timezone_for(legs[-1].destination))
    elapsed: int | None = None
    if start is not None and end is not None:
        minutes = int((end - start).total_seconds() // 60)
        if minutes > 0:
            elapsed = minutes
    if elapsed is not None:
        duration_minutes = elapsed
        basis = "elapsed"
    elif air_minutes > 0:
        duration_minutes = air_minutes
        basis = "air"
    else:
        return None

    carbon_raw = getattr(getattr(raw, "carbon", None), "emission", None)
    carbon = carbon_raw if isinstance(carbon_raw, int) and not isinstance(carbon_raw, bool) and carbon_raw >= 0 else None

    try:
        return Itinerary(
            price=price,
            currency=currency,
            airlines=airlines,
            airline_code=_airline_code(getattr(raw, "type", None)),
            depart_local=legs[0].depart_local,
            arrive_local=legs[-1].arrive_local,
            duration_minutes=duration_minutes,
            duration_basis=basis,  # type: ignore[arg-type]
            duration=_minutes_label(duration_minutes),
            stops=len(legs) - 1,
            route=route,
            legs=legs,
            search_url=search_url,
            carbon_emission_grams=carbon,
        )
    except Exception:
        # A shape we cannot validate is dropped, not patched into a fare.
        log.info("dropped itinerary that failed the result schema")
        return None


def _fail(code: str, message: str, **details: Any) -> FlightToolError:
    log.info("search failed code=%s", code)
    return FlightToolError(code, message, **details)


def run_search(
    *,
    origin: str,
    destination: str,
    depart_date: str,
    return_date: str | None = None,
    adults: int = 1,
    cabin: Cabin = "economy",
    currency: str = "CHF",
    max_results: int = DEFAULT_MAX_RESULTS,
    nonstop: bool = False,
    timeout_seconds: float = SEARCH_TIMEOUT_SECONDS,
    fetcher: Fetcher | None = None,
    cache: _Cache | None | object = _UNSET,
) -> SearchFlightsResult:
    """Search public fares. Raises FlightToolError. Never invents a price."""
    if cache is _UNSET:
        cache = None if fetcher is not None else _CACHE
    if cache is not None and not isinstance(cache, _Cache):
        raise TypeError("cache must be a Search cache or None")
    try:
        origin_place = pick_place(origin)
        destination_place = pick_place(destination)
    except PlaceError as exc:
        log.info("search failed code=%s", exc.code)
        raise place_error_to_tool(exc) from exc

    if origin_place.iata == destination_place.iata:
        raise _fail(
            "invalid_input",
            "Origin and destination resolve to the same place.",
            origin=origin_place.iata,
            destination=destination_place.iata,
        )
    if return_date is not None and return_date < depart_date:
        raise _fail(
            "invalid_input",
            "return_date must be on or after depart_date.",
            depart_date=depart_date,
            return_date=return_date,
        )

    if isinstance(adults, bool) or not isinstance(adults, int) or not 1 <= adults <= MAX_ADULTS:
        raise _fail("invalid_input", f"adults must be an integer from 1 to {MAX_ADULTS}.")
    if isinstance(max_results, bool) or not isinstance(max_results, int) or not 1 <= max_results <= MAX_RESULTS:
        raise _fail("invalid_input", f"max_results must be an integer from 1 to {MAX_RESULTS}.")
    if cabin not in ("economy", "premium-economy", "business", "first"):
        raise _fail("invalid_input", "cabin must be economy, premium-economy, business, or first.")
    adults_bounded = adults
    result_cap = max_results
    currency_code = currency.upper()
    trip = "round-trip" if return_date else "one-way"
    max_stops = 0 if nonstop else None

    key = (
        origin_place.iata,
        destination_place.iata,
        depart_date,
        return_date or "",
        adults_bounded,
        cabin,
        currency_code,
        result_cap,
        bool(nonstop),
    )
    if cache is not None:
        cached = cache.get(key)
        if cached is not None:
            log.info(
                "search %s->%s %s results=%s cache=hit",
                origin_place.iata,
                destination_place.iata,
                depart_date,
                cached.count,
            )
            return cached

    flight_queries = [
        FlightQuery(
            date=depart_date,
            from_airport=origin_place.iata,
            to_airport=destination_place.iata,
            max_stops=max_stops,
        )
    ]
    if return_date:
        flight_queries.append(
            FlightQuery(
                date=return_date,
                from_airport=destination_place.iata,
                to_airport=origin_place.iata,
                max_stops=max_stops,
            )
        )
    try:
        query = create_query(
            flights=flight_queries,
            seat=cabin,
            trip=trip,  # type: ignore[arg-type]
            passengers=Passengers(adults=adults_bounded),
            currency=currency_code,  # type: ignore[arg-type]
            language="en-US",
            max_stops=max_stops,
        )
    except Exception as exc:
        raise _fail("upstream", f"Could not build the search ({type(exc).__name__}).") from exc

    url = _search_url(
        query,
        origin=origin_place.iata,
        destination=destination_place.iata,
        depart_date=depart_date,
        return_date=return_date,
        currency=currency_code,
    )
    fetch = fetcher or fetch_results
    started = time.monotonic()
    try:
        if fetcher is None:
            raw_results = fetch(query, timeout_seconds=timeout_seconds)
        else:
            raw_results = fetch(query)
    except TimeoutError as exc:
        raise _fail(
            "timeout",
            "Google Flights did not answer before the timeout.",
            origin=origin_place.iata,
            destination=destination_place.iata,
            depart_date=depart_date,
            search_url=url,
        ) from exc
    except FlightsNotFound as exc:
        raise _fail(
            "no_results",
            f"No flights found for {origin_place.iata} to {destination_place.iata} on {depart_date}.",
            origin=origin_place.iata,
            destination=destination_place.iata,
            depart_date=depart_date,
            return_date=return_date,
            search_url=url,
        ) from exc
    except UpstreamFailure as exc:
        raise _fail(
            exc.code,
            str(exc),
            origin=origin_place.iata,
            destination=destination_place.iata,
            depart_date=depart_date,
            search_url=url,
        ) from exc
    except FlightToolError:
        raise
    except Exception as exc:
        raise _fail(
            "upstream",
            f"Google Flights request failed ({type(exc).__name__}).",
            origin=origin_place.iata,
            destination=destination_place.iata,
            depart_date=depart_date,
            search_url=url,
        ) from exc

    itineraries: list[Itinerary] = []
    saw_unpriced = False
    for item in raw_results:
        if nonstop and len(getattr(item, "flights", None) or []) != 1:
            continue
        built = _itinerary(item, currency=currency_code, search_url=url)
        if built is None:
            if _price(getattr(item, "price", None)) is None and getattr(item, "flights", None):
                saw_unpriced = True
            continue
        itineraries.append(built)
        if len(itineraries) >= result_cap:
            break

    if not itineraries:
        code = "unpriced" if saw_unpriced else "no_results"
        message = (
            "Google Flights returned itineraries without a usable price. No fare was invented."
            if saw_unpriced
            else f"No priced itineraries for {origin_place.iata} to {destination_place.iata} on {depart_date}."
        )
        raise _fail(
            code,
            message,
            origin=origin_place.iata,
            destination=destination_place.iata,
            depart_date=depart_date,
            return_date=return_date,
            search_url=url,
        )

    notes = list(_NOTES)
    if return_date:
        notes.append("For a round trip, price is the total Google showed. Legs are the segments in that result.")
    if saw_unpriced:
        notes.append("Itineraries without a numeric price were omitted.")

    result = SearchFlightsResult(
        origin=origin_place.iata,
        origin_name=origin_place.name,
        destination=destination_place.iata,
        destination_name=destination_place.name,
        depart_date=depart_date,
        return_date=return_date,
        adults=adults_bounded,
        cabin=cabin,
        currency=currency_code,
        nonstop=bool(nonstop),
        count=len(itineraries),
        cached=False,
        source="google_flights",
        search_url=url,
        flights=itineraries,
        notes=notes,
    )
    if cache is not None:
        cache.put(key, result)
    elapsed_ms = int((time.monotonic() - started) * 1000)
    log.info(
        "search %s->%s %s results=%s elapsed_ms=%s cache=miss",
        origin_place.iata,
        destination_place.iata,
        depart_date,
        result.count,
        elapsed_ms,
    )
    return result
