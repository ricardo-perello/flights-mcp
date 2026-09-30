# flights

A local [MCP](https://modelcontextprotocol.io) server for live flight search.
It speaks stdio, needs no API key, and prices fares from public Google Flights
pages through the unofficial [`fast-flights`](https://github.com/AWeirdDev/flights)
client. Airport names are resolved offline with `airportsdata`.

Defaults are **1 adult**, **economy**, **CHF**.

## Run

From this directory, with [uv](https://docs.astral.sh/uv/):

```bash
uv sync
uv run flights-mcp
```

The same process is `uv run python -m flights_mcp`. A host that does not use
uv can launch the module with whatever Python has the package installed:

```bash
python -m flights_mcp
```

The process is silent on stdout. MCP messages are the stdout stream. Logs go
to stderr.

Example host config (Cursor, Claude Desktop, and anything else that launches
a local stdio server):

```json
{
  "mcpServers": {
    "flights": {
      "command": "uv",
      "args": ["run", "--directory", "/absolute/path/to/this/repo", "flights-mcp"]
    }
  }
}
```

No environment variables are required.

## Tools

### `search_flights`

Search live public fares.

| Field | Required | Default | Bounds |
| --- | --- | --- | --- |
| `origin` | yes | | IATA code or city name, 1–80 characters |
| `destination` | yes | | same |
| `depart_date` | yes | | `YYYY-MM-DD`, today through 330 days, Europe/Zurich |
| `return_date` | no | one-way | same, on or after `depart_date` |
| `adults` | no | `1` | 1–9 |
| `cabin` | no | `economy` | `economy`, `premium-economy`, `business`, `first` |
| `currency` | no | `CHF` | a currency Google Flights prices in (`EUR`, `USD`, `GBP`, …) |
| `max_results` | no | `8` | 1–20 |
| `nonstop` | no | `false` | direct flights only |

Each itinerary includes the price and currency, airline names, local times,
duration, stops, the route, each leg, and a Google Flights search URL.
`flight_numbers` is not a field: this source does not provide them.

A city that matches several airports (Springfield) is an error. Pass an IATA
code, or a metro code such as `NYC`. `London` and `New York` resolve to the
metro code so the search covers the city's airports.

Unknown arguments are rejected. A failure is a tool error whose text contains
JSON `{"code","message",...}`. Codes include `unknown_place`,
`ambiguous_place`, `invalid_input`, `no_results`, `unpriced`, `timeout`, and
`upstream`. An error never carries an invented price.

### `resolve_airports`

Offline IATA lookup.

| Field | Required | Default | Bounds |
| --- | --- | --- | --- |
| `query` | yes | | city, airport name, or IATA code |
| `limit` | no | `10` | 1–20 |

`Zurich` and `Zürich` resolve to ZRH. `Barcelona` resolves to BCN (Barcelona
in Venezuela is BLA; the airport named for the city is the one a search
uses). `NYC` returns the metro code plus JFK and LGA. Newark is EWR.

## Limits

- Fares are snapshots. They can change before anyone books.
- No flight numbers, baggage allowances, seat maps, or fare rules.
- Times are local to each airport and have no offset. `duration_minutes` is
  door-to-door when both endpoint timezones are known (`duration_basis` is
  `elapsed`). Otherwise it is the sum of airborne minutes (`air`).
- For a round trip, `price` is the total Google showed. `legs` are the
  segments included in that result.
- The price is the integer Google returned, in the currency you asked for,
  in major units. It is not converted or rounded. Itineraries with no usable
  price are omitted. If none remain, the tool errors with `unpriced`.
- Identical searches in one process are reused for 2 minutes. The cache is
  memory only.
- Each search gives Google 25 seconds, then times out.
- At most two searches run at once.

Read [SECURITY.md](SECURITY.md) before pointing anything automated at this.
`fast-flights` can break when Google changes the page, it can be rate
limited, and automated access may violate Google's terms.

## Tests

Offline tests cover schemas, validation, airport resolution, and a search
that never leaves the machine:

```bash
uv run pytest
```

The live check is opt-in. It searches ZRH to BCN, one adult, economy, CHF,
about four weeks out:

```bash
FLIGHTS_LIVE=1 uv run pytest tests/test_live.py -s
```

### Live check

Recorded from a real search on this tree (not a fixture):

<!-- live-check:start -->
Not recorded yet.
<!-- live-check:end -->

## Layout

- `flights_mcp/server.py` — MCP tools and stdio entrypoint
- `flights_mcp/search.py` — query, timeout, cache, priced itineraries only
- `flights_mcp/upstream.py` — one bounded Google Flights fetch
- `flights_mcp/airports.py` — local IATA lookup
- `flights_mcp/models.py` — strict input and output schemas
