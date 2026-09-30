"""Live search over a capped set of dates. Prices come only from Google."""

from __future__ import annotations

import logging
import time
from collections import OrderedDict
from datetime import date, datetime, timedelta
from threading import Lock
from typing import Any, Callable
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from fast_flights import FlightQuery, FlightsNotFound, Passengers, create_query
from fast_flights.querying import Query

from flights_mcp.airports import PlaceError, pick_place, place_error_to_tool, timezone_for
from flights_mcp.dates import MAX_DATES, select_dates
from flights_mcp.errors import FlightToolError
from flights_mcp.google_parse import ParsedItinerary, ParsedLayover
from flights_mcp.models import (
    DEFAULT_MAX_RESULTS,
    MAX_ADULTS,
    MAX_RESULTS,
    Cabin,
    DayQuote,
    FlightLeg,
    Itinerary,
    Layover,
    Picks,
    SearchFlightsResult,
    parse_flight_date,
    zurich_today,
)
from flights_mcp.upstream import UpstreamFailure, fetch_results

log = logging.getLogger("flights_mcp")

SEARCH_TIMEOUT_SECONDS = 25.0
CACHE_TTL_SECONDS = 120.0
CACHE_MAX_ENTRIES = 32
MAX_FLEXIBILITY_DAYS = 3
PER_DAY_KEEP = 4

FARE_NOTE = (
    "Google Flights did not attach carry-on, checked bag, or fare family to this "
    "itinerary. Those fields are unknown. Nothing was assumed."
)
_NOTES = (
    "Fares are a live public snapshot and can change before you book.",
    "At most 7 departure dates are searched, inside one 25 second budget.",
    "Carry-on, checked bag, and fare family are unknown unless a future payload says otherwise.",
    "Times are local to each airport and have no timezone offset.",
    "A booking URL opens the Google Flights itinerary. It is not a ticket.",
)

Fetcher = Callable[[str, str | None], list[ParsedItinerary]]
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


def _aware(stamp: str, tz_name: str | None) -> datetime | None:
    if not tz_name:
        return None
    try:
        return datetime.fromisoformat(stamp).replace(tzinfo=ZoneInfo(tz_name))
    except (ValueError, KeyError, OSError):
        return None


def _fail(code: str, message: str, **details: Any) -> FlightToolError:
    log.info("search failed code=%s", code)
    return FlightToolError(code, message, **details)


def _parse_bounds(value: str | None, field: str) -> date | None:
    if value is None:
        return None
    try:
        parse_flight_date(value)
    except ValueError as exc:
        raise _fail("invalid_input", f"{field}: {exc}") from exc
    return date.fromisoformat(value)


def _departure_dates(
    *,
    depart_date: str | None,
    depart_from: str | None,
    depart_to: str | None,
    flexibility_days: int,
) -> tuple[str | None, date, date, list[date], list[date]]:
    if isinstance(flexibility_days, bool) or not isinstance(flexibility_days, int):
        raise _fail("invalid_input", "flexibility_days must be an integer from 0 to 3.")
    if not 0 <= flexibility_days <= MAX_FLEXIBILITY_DAYS:
        raise _fail("invalid_input", "flexibility_days must be from 0 to 3.")
    has_anchor = depart_date is not None
    has_range = depart_from is not None or depart_to is not None
    if has_anchor and has_range:
        raise _fail("invalid_input", "Pass depart_date or depart_from/depart_to, not both.")
    if not has_anchor and not has_range:
        raise _fail("invalid_input", "Pass depart_date, or depart_from and depart_to.")
    if has_range and (depart_from is None or depart_to is None):
        raise _fail("invalid_input", "depart_from and depart_to are both required for a date window.")
    if has_range and flexibility_days:
        raise _fail("invalid_input", "flexibility_days applies to depart_date, not to a from/to window.")

    if has_anchor:
        anchor = _parse_bounds(depart_date, "depart_date")
        assert anchor is not None
        start = anchor - timedelta(days=flexibility_days)
        end = anchor + timedelta(days=flexibility_days)
        today = zurich_today()
        horizon = today + timedelta(days=330)
        if start < today:
            start = today
        if end > horizon:
            end = horizon
        if end < start:
            raise _fail("invalid_input", "The flexible window has no dates on or after today.")
        searched, omitted = select_dates(start, end, cap=MAX_DATES)
        return depart_date, start, end, searched, omitted

    start = _parse_bounds(depart_from, "depart_from")
    end = _parse_bounds(depart_to, "depart_to")
    assert start is not None and end is not None
    if end < start:
        raise _fail("invalid_input", "depart_to must be on or after depart_from.")
    searched, omitted = select_dates(start, end, cap=MAX_DATES)
    return None, start, end, searched, omitted


def _reject_return_before_every_departure(return_date: str | None, searched: list[date]) -> None:
    if return_date is None:
        return
    if all(return_date < day.isoformat() for day in searched):
        raise _fail(
            "invalid_input",
            "return_date must be on or after at least one departure date.",
            return_date=return_date,
        )


def _search_url(
    query: Query,
    *,
    origin: str,
    destination: str,
    depart_date: str,
    return_date: str | None,
    currency: str,
) -> str:
    try:
        url = query.url()
    except Exception:
        url = ""
    if isinstance(url, str) and url.startswith("https://www.google.com/travel/flights") and len(url) <= 2000:
        return url
    text = f"Flights to {destination} from {origin} on {depart_date}"
    if return_date:
        text += f" through {return_date}"
    return "https://www.google.com/travel/flights?" + urlencode({"q": text, "curr": currency, "hl": "en"})


def _booking_url(token: str | None, currency: str) -> str | None:
    if not token:
        return None
    url = "https://www.google.com/travel/flights/booking?" + urlencode(
        {"tfs": token, "hl": "en-US", "curr": currency}
    )
    if len(url) > 2000:
        return None
    return url


def _schedule_gap(arrive_local: str, arrive_airport: str, depart_local: str, depart_airport: str) -> int | None:
    start = _aware(arrive_local, timezone_for(arrive_airport))
    end = _aware(depart_local, timezone_for(depart_airport))
    if start is None or end is None:
        return None
    minutes = int((end - start).total_seconds() // 60)
    if minutes < 0 or minutes > 60 * 72:
        return None
    return minutes


def _layovers(item: ParsedItinerary) -> list[Layover]:
    stops = len(item.legs) - 1
    if stops <= 0:
        return []
    indexed: dict[str, ParsedLayover] = {}
    for layover in item.layovers:
        indexed.setdefault(layover.airport, layover)
    built: list[Layover] = []
    for index in range(stops):
        left = item.legs[index]
        right = item.legs[index + 1]
        source = indexed.get(left.destination)
        if source is not None and source.minutes is not None:
            built.append(
                Layover(
                    airport=source.airport,
                    airport_name=source.airport_name,
                    city=source.city,
                    minutes=source.minutes,
                    minutes_basis="google",
                )
            )
            continue
        minutes = _schedule_gap(left.arrive_local, left.destination, right.depart_local, right.origin)
        built.append(
            Layover(
                airport=left.destination,
                airport_name=(source.airport_name if source else left.destination_name),
                city=source.city if source else None,
                minutes=minutes,
                minutes_basis="schedule" if minutes is not None else "unknown",
            )
        )
    return built


def _itinerary(
    item: ParsedItinerary,
    *,
    currency: str,
    depart_date: str,
    return_date: str | None,
    search_url: str,
) -> Itinerary | None:
    if item.price is None or not item.legs:
        return None
    legs: list[FlightLeg] = []
    numbers: list[str] = []
    for leg in item.legs:
        missing = leg.flight_number is None
        legs.append(
            FlightLeg(
                origin=leg.origin,
                origin_name=leg.origin_name,
                destination=leg.destination,
                destination_name=leg.destination_name,
                depart_local=leg.depart_local,
                arrive_local=leg.arrive_local,
                duration_minutes=leg.duration_minutes,
                aircraft=leg.aircraft,
                airline=leg.airline_name,
                airline_code=leg.airline_code,
                flight_number=leg.flight_number,
                flight_number_note=None if not missing else "Google did not include a flight number for this leg.",
                codeshare_flight_number=leg.codeshare_flight_number,
            )
        )
        if leg.flight_number:
            numbers.append(leg.flight_number)
    route = [legs[0].origin, *[leg.destination for leg in legs]]
    if item.duration_minutes is not None:
        duration_minutes = item.duration_minutes
        basis = "google"
    else:
        start = _aware(legs[0].depart_local, timezone_for(legs[0].origin))
        end = _aware(legs[-1].arrive_local, timezone_for(legs[-1].destination))
        elapsed = None
        if start is not None and end is not None:
            gap = int((end - start).total_seconds() // 60)
            if gap > 0:
                elapsed = gap
        if elapsed is not None:
            duration_minutes = elapsed
            basis = "elapsed"
        else:
            air = sum(leg.duration_minutes for leg in legs)
            if air <= 0:
                return None
            duration_minutes = air
            basis = "air"
    option_id = f"{depart_date}-{'-'.join(numbers) if numbers else '-'.join(route)}-{item.price}"
    if len(option_id) > 80:
        option_id = option_id[:80]
    try:
        return Itinerary(
            id=option_id,
            labels=[],
            depart_date=depart_date,
            return_date=return_date,
            price=item.price,
            currency=currency,
            airlines=item.airlines,
            airline_code=item.airline_code,
            flight_numbers=numbers,
            depart_local=legs[0].depart_local,
            arrive_local=legs[-1].arrive_local,
            duration_minutes=duration_minutes,
            duration_basis=basis,  # type: ignore[arg-type]
            duration=_minutes_label(duration_minutes),
            stops=len(legs) - 1,
            route=route,
            legs=legs,
            layovers=_layovers(item),
            carry_on="unknown",
            checked_bag="unknown",
            fare_family=None,
            fare_note=FARE_NOTE,
            search_url=search_url,
            booking_url=_booking_url(item.booking_token, currency),
            carbon_emission_grams=item.carbon_emission_grams,
        )
    except Exception:
        log.info("dropped itinerary that failed the result schema")
        return None


def _keep_day(options: list[Itinerary]) -> list[Itinerary]:
    if len(options) <= PER_DAY_KEEP:
        return options
    by_price = sorted(options, key=lambda item: (item.price, item.duration_minutes))
    kept = by_price[: PER_DAY_KEEP - 1]
    fastest = min(options, key=lambda item: (item.duration_minutes, item.price))
    if all(item.id != fastest.id for item in kept):
        kept.append(fastest)
    else:
        kept.append(by_price[PER_DAY_KEEP - 1])
    return kept


def _label(pool: list[Itinerary]) -> Picks:
    cheapest = min(pool, key=lambda item: (item.price, item.duration_minutes, item.id))
    fastest = min(pool, key=lambda item: (item.duration_minutes, item.price, item.id))
    prices = [item.price for item in pool]
    durations = [item.duration_minutes for item in pool]
    lo_price, hi_price = min(prices), max(prices)
    lo_duration, hi_duration = min(durations), max(durations)

    def score(item: Itinerary) -> float:
        price_span = hi_price - lo_price
        duration_span = hi_duration - lo_duration
        price_norm = 0.0 if price_span == 0 else (item.price - lo_price) / price_span
        duration_norm = 0.0 if duration_span == 0 else (item.duration_minutes - lo_duration) / duration_span
        return 0.5 * price_norm + 0.5 * duration_norm

    others = [item for item in pool if item.id not in {cheapest.id, fastest.id}]
    compromise = min(others, key=lambda item: (score(item), item.price, item.id)) if others else cheapest
    # One flight can be more than one of these. A dict keyed by id would keep only the last.
    merged: dict[str, list[str]] = {}
    for option_id, name in (
        (cheapest.id, "cheapest"),
        (fastest.id, "fastest"),
        (compromise.id, "compromise"),
    ):
        bucket = merged.setdefault(option_id, [])
        if name not in bucket:
            bucket.append(name)
    for item in pool:
        item.labels = merged.get(item.id, [])  # type: ignore[assignment]
    return Picks(cheapest=cheapest.id, fastest=fastest.id, compromise=compromise.id)


def _build_query(
    *,
    origin: str,
    destination: str,
    depart_date: str,
    return_date: str | None,
    adults: int,
    cabin: Cabin,
    currency: str,
    nonstop: bool,
) -> Query:
    max_stops = 0 if nonstop else None
    flights = [
        FlightQuery(date=depart_date, from_airport=origin, to_airport=destination, max_stops=max_stops)
    ]
    if return_date:
        flights.append(
            FlightQuery(date=return_date, from_airport=destination, to_airport=origin, max_stops=max_stops)
        )
    return create_query(
        flights=flights,
        seat=cabin,
        trip="round-trip" if return_date else "one-way",  # type: ignore[arg-type]
        passengers=Passengers(adults=adults),
        currency=currency,  # type: ignore[arg-type]
        language="en-US",
        max_stops=max_stops,
    )


def run_search(
    *,
    origin: str,
    destination: str,
    depart_date: str | None = None,
    depart_from: str | None = None,
    depart_to: str | None = None,
    flexibility_days: int = 0,
    return_date: str | None = None,
    nights: int | None = None,
    adults: int = 1,
    cabin: Cabin = "economy",
    currency: str = "CHF",
    max_results: int = DEFAULT_MAX_RESULTS,
    nonstop: bool = False,
    timeout_seconds: float = SEARCH_TIMEOUT_SECONDS,
    fetcher: Fetcher | None = None,
    cache: _Cache | None | object = _UNSET,
) -> SearchFlightsResult:
    """Search public fares across a capped date window. Never invents a price."""
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
    if isinstance(adults, bool) or not isinstance(adults, int) or not 1 <= adults <= MAX_ADULTS:
        raise _fail("invalid_input", f"adults must be an integer from 1 to {MAX_ADULTS}.")
    if isinstance(max_results, bool) or not isinstance(max_results, int) or not 1 <= max_results <= MAX_RESULTS:
        raise _fail("invalid_input", f"max_results must be an integer from 1 to {MAX_RESULTS}.")
    if cabin not in ("economy", "premium-economy", "business", "first"):
        raise _fail("invalid_input", "cabin must be economy, premium-economy, business, or first.")
    if nights is not None and (isinstance(nights, bool) or not isinstance(nights, int) or not 1 <= nights <= 30):
        raise _fail("invalid_input", "nights must be an integer from 1 to 30.")
    if return_date is not None and nights is not None:
        raise _fail("invalid_input", "Pass return_date or nights, not both.")
    if return_date is not None:
        _parse_bounds(return_date, "return_date")

    anchor, window_start, window_end, searched, omitted = _departure_dates(
        depart_date=depart_date,
        depart_from=depart_from,
        depart_to=depart_to,
        flexibility_days=flexibility_days,
    )
    _reject_return_before_every_departure(return_date, searched)
    currency_code = currency.upper()
    key = (
        origin_place.iata,
        destination_place.iata,
        anchor or "",
        window_start.isoformat(),
        window_end.isoformat(),
        tuple(day.isoformat() for day in searched),
        return_date or "",
        nights if nights is not None else -1,
        adults,
        cabin,
        currency_code,
        max_results,
        bool(nonstop),
    )
    if cache is not None:
        cached = cache.get(key)
        if cached is not None:
            log.info(
                "search %s->%s dates=%s results=%s cache=hit",
                origin_place.iata,
                destination_place.iata,
                len(searched),
                cached.count,
            )
            return cached

    deadline = time.monotonic() + float(timeout_seconds)
    started = time.monotonic()
    days: list[DayQuote] = []
    pool: list[Itinerary] = []
    saw_unpriced = False
    first_url = ""
    not_searched = [day.isoformat() for day in omitted]

    for day in searched:
        day_text = day.isoformat()
        day_return = return_date
        if nights is not None:
            day_return = (day + timedelta(days=nights)).isoformat()
        if day_return is not None and day_return < day_text:
            days.append(
                DayQuote(
                    date=day_text,
                    return_date=day_return,
                    status="skipped",
                    cheapest_price=None,
                    currency=currency_code,
                    itineraries=0,
                    detail="return_date is before this departure.",
                )
            )
            continue
        remaining = deadline - time.monotonic()
        if remaining < 1:
            not_searched.append(day_text)
            days.append(
                DayQuote(
                    date=day_text,
                    return_date=day_return,
                    status="timeout",
                    cheapest_price=None,
                    currency=currency_code,
                    itineraries=0,
                    detail="Stopped because the search timeout was reached.",
                )
            )
            continue
        try:
            query = _build_query(
                origin=origin_place.iata,
                destination=destination_place.iata,
                depart_date=day_text,
                return_date=day_return,
                adults=adults,
                cabin=cabin,
                currency=currency_code,
                nonstop=nonstop,
            )
        except Exception as exc:
            days.append(
                DayQuote(
                    date=day_text,
                    return_date=day_return,
                    status="upstream",
                    cheapest_price=None,
                    currency=currency_code,
                    itineraries=0,
                    detail=f"Could not build the search ({type(exc).__name__}).",
                )
            )
            continue
        url = _search_url(
            query,
            origin=origin_place.iata,
            destination=destination_place.iata,
            depart_date=day_text,
            return_date=day_return,
            currency=currency_code,
        )
        if not first_url:
            first_url = url
        try:
            if fetcher is None:
                raw_results = fetch_results(query, timeout_seconds=max(1.0, remaining))
            else:
                raw_results = fetcher(day_text, day_return)
        except (TimeoutError, UpstreamFailure) as exc:
            status = exc.code if isinstance(exc, UpstreamFailure) else "timeout"
            if status not in {"timeout", "upstream", "no_results", "unpriced"}:
                status = "upstream"
            days.append(
                DayQuote(
                    date=day_text,
                    return_date=day_return,
                    status=status,  # type: ignore[arg-type]
                    cheapest_price=None,
                    currency=currency_code,
                    itineraries=0,
                    detail=str(exc) if isinstance(exc, UpstreamFailure) else "Google Flights did not answer before the timeout.",
                )
            )
            continue
        except FlightsNotFound:
            days.append(
                DayQuote(
                    date=day_text,
                    return_date=day_return,
                    status="no_results",
                    cheapest_price=None,
                    currency=currency_code,
                    itineraries=0,
                    detail="No flights found.",
                )
            )
            continue
        except Exception:
            days.append(
                DayQuote(
                    date=day_text,
                    return_date=day_return,
                    status="upstream",
                    cheapest_price=None,
                    currency=currency_code,
                    itineraries=0,
                    detail="Google Flights request failed.",
                )
            )
            continue

        built: list[Itinerary] = []
        day_unpriced = False
        for raw in raw_results:
            if nonstop and len(raw.legs) != 1:
                continue
            if raw.price is None:
                if raw.legs:
                    day_unpriced = True
                continue
            option = _itinerary(
                raw,
                currency=currency_code,
                depart_date=day_text,
                return_date=day_return,
                search_url=url,
            )
            if option is None:
                continue
            built.append(option)
        if day_unpriced:
            saw_unpriced = True
        if not built:
            days.append(
                DayQuote(
                    date=day_text,
                    return_date=day_return,
                    status="unpriced" if day_unpriced else "no_results",
                    cheapest_price=None,
                    currency=currency_code,
                    itineraries=0,
                    detail=(
                        "Itineraries had no usable price."
                        if day_unpriced
                        else "No priced itineraries."
                    ),
                )
            )
            continue
        kept = _keep_day(built)
        cheapest_price = min(item.price for item in kept)
        days.append(
            DayQuote(
                date=day_text,
                return_date=day_return,
                status="ok",
                cheapest_price=cheapest_price,
                currency=currency_code,
                itineraries=len(built),
                detail=None,
            )
        )
        pool.extend(kept)

    if not pool:
        statuses = {day.status for day in days}
        if statuses and statuses <= {"timeout"}:
            raise _fail(
                "timeout",
                f"Search exceeded {int(timeout_seconds)} seconds.",
                origin=origin_place.iata,
                destination=destination_place.iata,
            )
        code = "unpriced" if saw_unpriced else "no_results"
        message = (
            "Google Flights returned itineraries without a usable price. No fare was invented."
            if saw_unpriced
            else f"No priced itineraries for {origin_place.iata} to {destination_place.iata} in the searched dates."
        )
        raise _fail(
            code,
            message,
            origin=origin_place.iata,
            destination=destination_place.iata,
            dates_searched=[day.isoformat() for day in searched],
        )

    picks = _label(pool)
    chosen = {picks.cheapest, picks.fastest, picks.compromise}
    ordered = sorted(pool, key=lambda item: (item.price, item.duration_minutes, item.depart_date, item.id))
    selected: list[Itinerary] = []
    seen: set[str] = set()
    for item in ordered:
        if item.id in chosen or len(selected) < max_results:
            if item.id in seen:
                continue
            selected.append(item)
            seen.add(item.id)
        if len(selected) >= max_results and chosen <= seen:
            break
    # Labels were applied on the pool objects, which are the same instances.
    notes = list(_NOTES)
    if return_date or nights is not None:
        notes.append("For a round trip, price is the total Google showed for that departure and return.")
    if omitted or any(day.status == "timeout" for day in days):
        notes.append("Some dates in the window were not searched. See dates_not_searched and days.")
    if saw_unpriced:
        notes.append("Itineraries without a numeric price were omitted.")

    result = SearchFlightsResult(
        origin=origin_place.iata,
        origin_name=origin_place.name,
        destination=destination_place.iata,
        destination_name=destination_place.name,
        depart_date=anchor,
        depart_from=window_start.isoformat(),
        depart_to=window_end.isoformat(),
        dates_searched=[day.isoformat() for day in searched if day.isoformat() not in not_searched],
        dates_not_searched=not_searched,
        return_date=return_date,
        nights=nights,
        adults=adults,
        cabin=cabin,
        currency=currency_code,
        nonstop=bool(nonstop),
        count=len(selected),
        cached=False,
        source="google_flights",
        search_url=first_url or "https://www.google.com/travel/flights",
        days=days,
        picks=picks,
        flights=selected,
        notes=notes[:12],
    )
    if cache is not None:
        cache.put(key, result)
    log.info(
        "search %s->%s dates=%s results=%s elapsed_ms=%s cache=miss",
        origin_place.iata,
        destination_place.iata,
        len(searched),
        result.count,
        int((time.monotonic() - started) * 1000),
    )
    return result
