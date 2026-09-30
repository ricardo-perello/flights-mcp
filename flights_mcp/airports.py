"""Offline airport and city resolution.

The lookup is the airportsdata snapshot shipped with the package. Nothing
here touches the network.
"""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Any

import airportsdata

from flights_mcp.errors import FlightToolError
from flights_mcp.models import AirportMatch, normalize_name

_IATA_RE = re.compile(r"^[A-Za-z]{3}$")
_EXACT_SCORE = 100
_WORD_SCORE = 80
_PREFIX_SCORE = 70


class PlaceError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        query: str,
        candidates: list[AirportMatch] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.query = query
        self.candidates = candidates or []


def place_error_to_tool(exc: PlaceError) -> FlightToolError:
    preview = [
        {
            "iata": match.iata,
            "name": match.name,
            "country": match.country,
            "kind": match.kind,
        }
        for match in exc.candidates[:5]
    ]
    return FlightToolError(
        exc.code,
        exc.message,
        query=exc.query,
        candidates=preview or None,
    )


@lru_cache(maxsize=1)
def _databases() -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    return airportsdata.load("IATA"), airportsdata.load_iata_macs()


def _clip(value: Any, limit: int) -> str:
    text = str(value or "").strip()
    return text[:limit]


def _airport_match(code: str, info: dict[str, Any]) -> AirportMatch | None:
    iata = code.upper()
    if len(iata) != 3 or not iata.isalnum():
        return None
    return AirportMatch(
        iata=code.upper(),
        name=_clip(info.get("name") or code, 160),
        city=_clip(info.get("city"), 80),
        country=_clip(info.get("country"), 2),
        timezone=_clip(info.get("tz"), 64),
        kind="airport",
    )


def _metro_members(mac: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    airports = mac.get("airports") or {}
    if not isinstance(airports, dict):
        return []
    return [(str(code).upper(), info) for code, info in airports.items() if isinstance(info, dict)]


def _metro_match(code: str, mac: dict[str, Any]) -> AirportMatch | None:
    iata = code.upper()
    if len(iata) != 3 or not iata.isalnum():
        return None
    members = _metro_members(mac)
    timezones = {str(info.get("tz") or "") for _, info in members}
    timezones.discard("")
    timezone = next(iter(timezones)) if len(timezones) == 1 else ""
    country = _clip(mac.get("country"), 2)
    if not country and members:
        country = _clip(members[0][1].get("country"), 2)
    return AirportMatch(
        iata=iata,
        name=_clip(mac.get("name") or code, 160),
        city=_clip(mac.get("name") or "", 80),
        country=country,
        timezone=timezone,
        kind="metro",
    )


def timezone_for(iata: str) -> str | None:
    airports, metros = _databases()
    info = airports.get(iata.upper())
    if info:
        tz = str(info.get("tz") or "").strip()
        return tz or None
    mac = metros.get(iata.upper())
    if not mac:
        return None
    zones = {str(item.get("tz") or "").strip() for _, item in _metro_members(mac)}
    zones.discard("")
    if len(zones) == 1:
        return next(iter(zones))
    return None


def _score_text(query: str, text: str) -> int:
    if not text:
        return 0
    if text == query:
        return _EXACT_SCORE
    words = text.split()
    if query in words:
        return _WORD_SCORE
    if text.startswith(query + " ") or text.startswith(query):
        return _PREFIX_SCORE
    return 0


def _ranked(query: str, limit: int) -> list[tuple[int, AirportMatch]]:
    """Rank matches. Higher score wins. Exact codes short-circuit."""
    airports, metros = _databases()
    raw = query.strip()
    if _IATA_RE.fullmatch(raw):
        code = raw.upper()
        if code in airports:
            match = _airport_match(code, airports[code])
            return [(_EXACT_SCORE, match)] if match is not None else []
        if code in metros:
            mac = metros[code]
            metro = _metro_match(code, mac)
            if metro is None:
                return []
            ranked: list[tuple[int, AirportMatch]] = [(_EXACT_SCORE, metro)]
            for apt_code, info in _metro_members(mac):
                source = airports.get(apt_code, info)
                match = _airport_match(apt_code, source)
                if match is not None:
                    ranked.append((_EXACT_SCORE - 1, match))
            return ranked[:limit]

    norm = normalize_name(raw)
    if not norm:
        return []

    best: dict[str, tuple[int, AirportMatch]] = {}

    def keep(score: int, match: AirportMatch | None) -> None:
        if score <= 0 or match is None:
            return
        current = best.get(match.iata)
        if current is None or score > current[0]:
            best[match.iata] = (score, match)

    for code, mac in metros.items():
        name_score = _score_text(norm, normalize_name(str(mac.get("name") or "")))
        if name_score:
            # An exact metro name outranks a single airport with the same city.
            boost = 5 if name_score == _EXACT_SCORE else 0
            keep(name_score + boost, _metro_match(code, mac))

    for code, info in airports.items():
        city_score = _score_text(norm, normalize_name(str(info.get("city") or "")))
        name_score = _score_text(norm, normalize_name(str(info.get("name") or "")))
        keep(max(city_score, name_score), _airport_match(code, info))

    ranked = sorted(best.values(), key=lambda item: (-item[0], item[1].kind != "metro", item[1].iata))
    return ranked[:limit]


def resolve_airports(query: str, *, limit: int = 10) -> list[AirportMatch]:
    """Return up to `limit` matches. Empty when nothing is close."""
    bounded = max(1, min(int(limit), 20))
    return [match for _, match in _ranked(query, bounded)]


def pick_place(query: str) -> AirportMatch:
    """Choose one code for a search, or raise PlaceError."""
    cleaned = " ".join(query.split())
    if not cleaned:
        raise PlaceError("unknown_place", "Origin and destination must not be empty.", query=query)

    ranked = _ranked(cleaned, limit=8)
    if not ranked:
        raise PlaceError(
            "unknown_place",
            f"No airport matched {cleaned!r}. Try an IATA code or a city name.",
            query=cleaned,
        )

    top_score, top = ranked[0]
    if _IATA_RE.fullmatch(cleaned):
        return top

    exact = [match for score, match in ranked if score >= _EXACT_SCORE]
    metros = [match for match in exact if match.kind == "metro"]
    airports = [match for match in exact if match.kind == "airport"]
    if len(metros) == 1:
        return metros[0]
    if len(metros) > 1:
        raise PlaceError(
            "ambiguous_place",
            f"{cleaned!r} matches more than one city. Pass an IATA code.",
            query=cleaned,
            candidates=metros,
        )
    if len(airports) == 1:
        return airports[0]
    if len(airports) > 1:
        # The same city string can exist in two countries (Barcelona, Spain
        # and Barcelona, Venezuela). If only one airport is actually named
        # for that city, it is the one a person means.
        city_key = normalize_name(cleaned)
        named_for_city = [
            match
            for match in airports
            if normalize_name(match.name) == city_key
            or normalize_name(match.name).startswith(city_key + " ")
        ]
        countries = {match.country for match in airports}
        if len(countries) > 1 and len(named_for_city) == 1:
            return named_for_city[0]
        raise PlaceError(
            "ambiguous_place",
            f"{cleaned!r} has several airports. Pass an IATA code.",
            query=cleaned,
            candidates=airports,
        )

    second = ranked[1][0] if len(ranked) > 1 else 0
    if top_score >= _WORD_SCORE and top_score - second >= 15:
        return top

    preview = [match for _, match in ranked[:5]]
    if top_score >= 60:
        raise PlaceError(
            "ambiguous_place",
            f"{cleaned!r} is ambiguous. Pass an IATA code.",
            query=cleaned,
            candidates=preview,
        )
    raise PlaceError(
        "unknown_place",
        f"No airport matched {cleaned!r}. Try an IATA code or a city name.",
        query=cleaned,
        candidates=preview,
    )
