"""Schema, validation, and in-memory MCP calls. No network."""

import asyncio
import json

from mcp import Client

from flights_mcp.server import mcp
from tests.support import depart_in, zurich_barcelona


def _run(coro):
    return asyncio.run(coro)


def _tools():
    async def go():
        async with Client(mcp) as client:
            listed = await client.list_tools()
            return {tool.name: tool for tool in listed.tools}

    return _run(go())


def _assert_closed_objects(schema: dict) -> None:
    if schema.get("type") == "object":
        assert schema.get("additionalProperties") is False, schema.get("title")
    for nested in (schema.get("$defs") or {}).values():
        _assert_closed_objects(nested)
    items = schema.get("items")
    if isinstance(items, dict):
        _assert_closed_objects(items)
    for key in ("anyOf", "oneOf", "allOf"):
        for option in schema.get(key) or []:
            if isinstance(option, dict):
                _assert_closed_objects(option)


def test_tools_are_listed_with_strict_schemas():
    tools = _tools()
    assert set(tools) == {"search_flights", "resolve_airports"}

    search = tools["search_flights"]
    assert search.annotations is not None
    assert search.annotations.read_only_hint is True
    assert search.annotations.open_world_hint is True
    params = search.input_schema
    assert params["type"] == "object"
    assert params["additionalProperties"] is False
    assert set(params["required"]) == {"origin", "destination", "depart_date"}
    assert params["properties"]["adults"]["default"] == 1
    assert params["properties"]["cabin"]["default"] == "economy"
    assert params["properties"]["currency"]["default"] == "CHF"
    assert params["properties"]["max_results"]["maximum"] == 20
    assert params["properties"]["adults"]["maximum"] == 9
    assert "economy" in params["properties"]["cabin"]["enum"]
    _assert_closed_objects(search.output_schema)
    assert "price" in search.output_schema["$defs"]["Itinerary"]["properties"]
    assert "flight_numbers" not in search.output_schema["$defs"]["Itinerary"]["properties"]

    resolve = tools["resolve_airports"]
    assert resolve.input_schema["additionalProperties"] is False
    assert resolve.annotations.open_world_hint is False
    _assert_closed_objects(resolve.output_schema)


def test_resolve_zurich_and_barcelona_over_mcp():
    async def go():
        async with Client(mcp) as client:
            zurich = await client.call_tool("resolve_airports", {"query": "Zurich"})
            barcelona = await client.call_tool("resolve_airports", {"query": "Barcelona"})
            return zurich, barcelona

    zurich, barcelona = _run(go())
    assert zurich.is_error is False
    assert barcelona.is_error is False
    assert zurich.structured_content["airports"][0]["iata"] == "ZRH"
    assert barcelona.structured_content["airports"][0]["iata"] == "BCN"
    assert zurich.structured_content["airports"][0]["country"] == "CH"
    assert barcelona.structured_content["airports"][0]["country"] == "ES"


def test_search_defaults_and_structured_result(monkeypatch):
    day = depart_in(28)

    def fetch(query, timeout_seconds: float):
        return [zurich_barcelona(day, price=108)]

    monkeypatch.setattr("flights_mcp.search.fetch_results", fetch)

    async def go():
        async with Client(mcp) as client:
            return await client.call_tool(
                "search_flights",
                {"origin": "Zurich", "destination": "Barcelona", "depart_date": day},
            )

    result = _run(go())
    assert result.is_error is False
    body = result.structured_content
    assert body["origin"] == "ZRH"
    assert body["destination"] == "BCN"
    assert body["adults"] == 1
    assert body["cabin"] == "economy"
    assert body["currency"] == "CHF"
    assert body["nonstop"] is False
    assert body["source"] == "google_flights"
    assert body["flights"][0]["price"] == 108
    assert body["flights"][0]["airlines"] == ["SWISS"]
    assert body["flights"][0]["stops"] == 0
    assert "flight_numbers" not in body["flights"][0]


def test_bad_arguments_and_extra_fields_are_rejected():
    day = depart_in(21)

    async def go():
        async with Client(mcp) as client:
            extra = await client.call_tool(
                "search_flights",
                {
                    "origin": "ZRH",
                    "destination": "BCN",
                    "depart_date": day,
                    "fare_class": "Y",
                },
            )
            adults = await client.call_tool(
                "search_flights",
                {"origin": "ZRH", "destination": "BCN", "depart_date": day, "adults": 0},
            )
            cabin = await client.call_tool(
                "search_flights",
                {"origin": "ZRH", "destination": "BCN", "depart_date": day, "cabin": "coach"},
            )
            past = await client.call_tool(
                "search_flights",
                {"origin": "ZRH", "destination": "BCN", "depart_date": "2020-01-01"},
            )
            currency = await client.call_tool(
                "search_flights",
                {"origin": "ZRH", "destination": "BCN", "depart_date": day, "currency": "XXX"},
            )
            missing = await client.call_tool("resolve_airports", {"query": "Not A Real City Anywhere"})
            return extra, adults, cabin, past, currency, missing

    extra, adults, cabin, past, currency, missing = _run(go())
    for result in (extra, adults, cabin, past, currency, missing):
        assert result.is_error is True
        assert result.structured_content is None
    assert "unknown_place" in missing.content[0].text


def test_ambiguous_city_is_a_structured_search_error():
    day = depart_in(21)

    async def go():
        async with Client(mcp) as client:
            return await client.call_tool(
                "search_flights",
                {"origin": "Springfield", "destination": "ZRH", "depart_date": day},
            )

    result = _run(go())
    assert result.is_error is True
    text = result.content[0].text
    start = text.find("{")
    payload = json.loads(text[start:])
    assert payload["code"] == "ambiguous_place"
    assert payload["candidates"]


def test_browser_profile_used_for_live_search_is_constructible():
    from primp import Client as PrimpClient

    PrimpClient(
        impersonate="chrome_145",
        impersonate_os="macos",
        timeout=1.0,
        cookie_store=False,
    )
