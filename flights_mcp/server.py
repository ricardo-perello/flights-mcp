"""MCP server. stdio is the only transport this process serves.

Tool functions in this module must not use `from __future__ import annotations`.
The SDK reads the live annotations to build input and output schemas.
"""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from typing import Annotated

from pydantic import ConfigDict, Field
from mcp.server import MCPServer
from mcp.server.mcpserver.utilities.func_metadata import ArgModelBase
from mcp.types import ToolAnnotations

from flights_mcp import __version__
from flights_mcp.airports import resolve_airports
from flights_mcp.errors import FlightToolError
from flights_mcp.models import (
    DEFAULT_MAX_RESULTS,
    MAX_ADULTS,
    MAX_PLACE_LENGTH,
    MAX_RESULTS,
    RESOLVE_LIMIT_DEFAULT,
    RESOLVE_LIMIT_MAX,
    Cabin,
    CurrencyCode,
    FlightDate,
    PlaceName,
    ResolveAirportsResult,
    SearchFlightsResult,
)
from flights_mcp.search import SEARCH_TIMEOUT_SECONDS, run_search

_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="flight-search")

# The generated argument model ignores unknown keys unless this is set before
# the tools are registered. Unknown arguments are then a tool error.
ArgModelBase.model_config = ConfigDict(
    arbitrary_types_allowed=True,
    extra="forbid",
    strict=True,
)

INSTRUCTIONS = (
    "Live public flight search for planning a trip. Defaults are 1 adult, "
    "economy, CHF. Use resolve_airports to turn a city into an IATA code. "
    "Pass depart_date, or depart_date plus flexibility_days (0 to 3), or "
    "depart_from and depart_to for a window. At most 7 departure dates are "
    "searched; longer windows are sampled and the omitted dates are listed. "
    "Each option has price, duration, stops, local times, airlines, flight "
    "numbers when Google sent them, layovers, and a search or booking URL. "
    "picks.cheapest, picks.fastest, and picks.compromise name comparable "
    "options. Carry-on, checked bag, and fare family are unknown: Google "
    "does not attach them to the itinerary, so do not guess. Fares are "
    "snapshots. Never treat a missing price as zero."
)

mcp = MCPServer(
    name="flights",
    title="Flights",
    version=__version__,
    instructions=INSTRUCTIONS,
)


def _refresh_input_schema(model: type) -> None:
    """Publish additionalProperties: false and actually reject extra keys."""
    config = ConfigDict(arbitrary_types_allowed=True, extra="forbid", strict=True)
    model.model_config = config  # type: ignore[attr-defined]
    pydantic_config = getattr(model, "__pydantic_config__", None)
    if pydantic_config is not None:
        model.__pydantic_config__ = config  # type: ignore[attr-defined]
    model.model_rebuild(force=True)  # type: ignore[attr-defined]


@mcp.tool(
    title="Search flights",
    annotations=ToolAnnotations(
        read_only_hint=True,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=True,
    ),
)
async def search_flights(
    origin: PlaceName,
    destination: PlaceName,
    depart_date: Annotated[
        FlightDate | None,
        Field(
            description=(
                "Departure date YYYY-MM-DD. Use this, optionally with flexibility_days. "
                "Do not combine it with depart_from and depart_to."
            ),
        ),
    ] = None,
    depart_from: Annotated[
        FlightDate | None,
        Field(description="First departure date of a window, inclusive. Pair with depart_to."),
    ] = None,
    depart_to: Annotated[
        FlightDate | None,
        Field(
            description=(
                "Last departure date of a window, inclusive. At most 7 dates are searched; "
                "a longer window is sampled and the rest are listed as not searched."
            ),
        ),
    ] = None,
    flexibility_days: Annotated[
        int,
        Field(
            ge=0,
            le=3,
            description="Days before and after depart_date. 0 searches that day only. Not used with a from/to window.",
        ),
    ] = 0,
    return_date: Annotated[
        FlightDate | None,
        Field(description="Fixed return date YYYY-MM-DD. Omit for one-way. Do not combine with nights."),
    ] = None,
    nights: Annotated[
        int | None,
        Field(
            ge=1,
            le=30,
            description="Stay length in nights. The return date is each departure plus this many days. Do not combine with return_date.",
        ),
    ] = None,
    adults: Annotated[
        int,
        Field(ge=1, le=MAX_ADULTS, description="Adults, 1 to 9. Children are not supported."),
    ] = 1,
    cabin: Annotated[Cabin, Field(description="Seat cabin.")] = "economy",
    currency: CurrencyCode = "CHF",
    max_results: Annotated[
        int,
        Field(ge=1, le=MAX_RESULTS, description=f"How many itineraries to return, at most {MAX_RESULTS}."),
    ] = DEFAULT_MAX_RESULTS,
    nonstop: Annotated[bool, Field(description="When true, only nonstop itineraries.")] = False,
) -> SearchFlightsResult:
    """Search live public Google Flights fares across a short date window.

    origin and destination take an IATA code or a city name. Defaults are
    1 adult, economy, CHF. A planner can ask for one day, a few days around
    that day, or a from/to window such as a week. The price is the integer
    Google returned. This tool does not invent a fare, a flight number, or a bag rule.
    """
    try:
        return await asyncio.wait_for(
            asyncio.get_running_loop().run_in_executor(
                _EXECUTOR,
                partial(
                    run_search,
                    origin=origin,
                    destination=destination,
                    depart_date=depart_date,
                    depart_from=depart_from,
                    depart_to=depart_to,
                    flexibility_days=flexibility_days,
                    return_date=return_date,
                    nights=nights,
                    adults=adults,
                    cabin=cabin,
                    currency=currency,
                    max_results=max_results,
                    nonstop=nonstop,
                    timeout_seconds=SEARCH_TIMEOUT_SECONDS,
                ),
            ),
            timeout=SEARCH_TIMEOUT_SECONDS + 2,
        )
    except TimeoutError as exc:
        raise FlightToolError(
            "timeout",
            f"Search exceeded {int(SEARCH_TIMEOUT_SECONDS)} seconds.",
            origin=origin,
            destination=destination,
            depart_date=depart_date,
            depart_from=depart_from,
            depart_to=depart_to,
        ) from exc


@mcp.tool(
    name="resolve_airports",
    title="Resolve airports",
    annotations=ToolAnnotations(
        read_only_hint=True,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=False,
    ),
)
def resolve_airports_tool(
    query: Annotated[
        str,
        Field(
            min_length=1,
            max_length=MAX_PLACE_LENGTH,
            description="City, airport name, or IATA code. Examples: Zurich, BCN, NYC.",
        ),
    ],
    limit: Annotated[
        int,
        Field(ge=1, le=RESOLVE_LIMIT_MAX, description="Maximum number of matches."),
    ] = RESOLVE_LIMIT_DEFAULT,
) -> ResolveAirportsResult:
    """Resolve a city or airport name to IATA codes, offline.

    Zurich resolves to ZRH. Barcelona resolves to BCN. A metro code such as
    NYC returns that city code and its airports. The lookup does not use the network.
    """
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in query):
        raise FlightToolError("unknown_place", "query must not contain control characters.")
    cleaned = " ".join(query.split())
    if not cleaned:
        raise FlightToolError("unknown_place", "query must not be empty.", query=query)
    if len(cleaned) > MAX_PLACE_LENGTH:
        raise FlightToolError(
            "unknown_place",
            f"query must be at most {MAX_PLACE_LENGTH} characters.",
            query=query[:MAX_PLACE_LENGTH],
        )
    matches = resolve_airports(cleaned, limit=limit)
    if not matches:
        raise FlightToolError(
            "unknown_place",
            f"No airport matched {cleaned!r}.",
            query=cleaned,
        )
    return ResolveAirportsResult(query=cleaned, count=len(matches), airports=matches)


def _tighten_registered_inputs() -> None:
    for tool in mcp._tool_manager.list_tools():
        _refresh_input_schema(tool.fn_metadata.arg_model)


_tighten_registered_inputs()


def main() -> None:
    mcp.run(transport="stdio")
