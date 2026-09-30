# flights

A local [MCP](https://modelcontextprotocol.io) server for planning a trip from live public fares.
It speaks stdio, needs no API key, and reads Google Flights pages through the
unofficial [`fast-flights`](https://github.com/AWeirdDev/flights) query builder.
Airport names are resolved offline with `airportsdata`.

Defaults are **1 adult**, **economy**, **CHF**.

## Why this source

The constraint is a planner an agent can run with no paid account and no API
key, and with no invented prices. The fields that matter are flight numbers,
layovers, bags or fare family, a date window, and a fare that was actually
returned.

| Source | What it would take | What a planner gets |
| --- | --- | --- |
| Google Flights page, parsed here | No key. Unofficial, so it can break or be blocked. | Live CHF prices. Flight numbers and layover minutes are in the page payload. Bags and fare family are not attached to an itinerary. No date-price calendar, so a window is several bounded searches. |
| Kiwi Tequila | `api.tequila.kiwi.com` returns HTTP 403 without a key. | Not usable. |
| Amadeus, Duffel, Skyscanner partner APIs | A developer account and a key. | Out of scope. |
| A single airline API | No key for some public pages, but only that airline. | Misses the rest of a ZRH–BCN comparison. |
| Hosted date-grid sites (for example whentofly) | No key, but the fares are a hosted cache, not this process reading a live public page. | Not a local source, and not something to treat as a bookable fare. |

Google is the one that still lets an agent compare real itineraries. The
server says so when a field is missing. It does not fill bags in from
legroom inches or from unnamed numeric flags on the payload.

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

Search live public fares across a short set of departure dates.

| Field | Required | Default | Bounds |
| --- | --- | --- | --- |
| `origin` | yes | | IATA code or city name, 1–80 characters |
| `destination` | yes | | same |
| `depart_date` | one of the date forms | | `YYYY-MM-DD`, today through 330 days, Europe/Zurich |
| `flexibility_days` | no | `0` | 0–3 days either side of `depart_date` |
| `depart_from` | with `depart_to` | | first departure, inclusive |
| `depart_to` | with `depart_from` | | last departure, inclusive |
| `return_date` | no | one-way | fixed return, on or after a searched departure |
| `nights` | no | | 1–30. Return becomes each departure plus this many days. Not with `return_date` |
| `adults` | no | `1` | 1–9 |
| `cabin` | no | `economy` | `economy`, `premium-economy`, `business`, `first` |
| `currency` | no | `CHF` | a currency Google Flights prices in (`EUR`, `USD`, `GBP`, …) |
| `max_results` | no | `8` | 1–20 |
| `nonstop` | no | `false` | direct flights only |

Pass `depart_date`, or `depart_date` with `flexibility_days`, or
`depart_from` and `depart_to`. Not both an anchor and a from/to window.
"Zurich to Barcelona, sometime in a week" is `depart_from` and `depart_to`
seven days apart.

At most **7** departure dates are queried, one after another, inside **25
seconds**. A longer window is sampled, including the first and last day, and
the dates that were skipped are listed in `dates_not_searched`. A day that
runs out of time is recorded on `days` and is not given an invented fare.
`nights` does not search a grid of return dates. Each departure is one query.

Each itinerary includes:

- price and currency, as the integer Google returned
- airline names, local times, duration, stops, route, and each leg
- `flight_numbers` when Google sent them. A leg without one has `flight_number: null` and a note that says so
- layovers: airport and minutes. `minutes_basis` is `google` when the payload included the layover length, `schedule` when it was the gap between the leg times, and `unknown` when neither could be read. An empty `layovers` list means nonstop
- `carry_on`, `checked_bag`, and `fare_family`. On this source those are `unknown` / `null`, and `fare_note` says that nothing was assumed
- `search_url` for that date, and `booking_url` when Google attached a booking token. The booking link opens the itinerary on Google Flights. It is not a ticket

`picks.cheapest`, `picks.fastest`, and `picks.compromise` are itinerary ids.
`compromise` is the option with the best even balance of price and duration
among the flights that are not already the cheapest or the fastest. When the
pool is too small for a distinct third option, it matches one of the other
picks. The same id can carry more than one label. `flights` is sorted by price
and always includes the three picks, up to `max_results`.

A city that matches several airports (Springfield) is an error. Pass an IATA
code, or a metro code such as `NYC`. `London` and `New York` resolve to the
metro code so the search covers the city's airports.

Unknown arguments are rejected. A failure is a tool error whose text contains
JSON `{"code","message",...}`. Codes include `unknown_place`,
`ambiguous_place`, `invalid_input`, `no_results`, `unpriced`, `timeout`, and
`upstream`. An error never carries an invented price. A single day inside a
window can also be `skipped` when its return would fall before departure.

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
- Bags and fare family are not on the itinerary Google returns to this parser. The fields are present so a planner can see the absence. They are never guessed.
- Times are local to each airport and have no offset. `duration_basis` is `google` when the payload included the trip duration, `elapsed` when it was computed from airport timezones, and `air` when only airborne minutes were available.
- For a round trip, `price` is the total Google showed for that departure and return. `legs` are the segments included in that result.
- The price is the integer Google returned, in the currency you asked for, in major units. It is not converted or rounded. Itineraries with no usable price are omitted. If none remain, the tool errors with `unpriced`.
- Identical searches in one process are reused for 2 minutes. The cache is memory only and includes the date window.
- Each day contributes at most 4 itineraries to the comparison: the cheapest ones, plus that day's fastest if it was not already among them. The overall list is still capped by `max_results`, and the labeled options are kept inside that cap.
- The whole search, including every date in the window, gets 25 seconds.
- At most two searches run at once.

Read [SECURITY.md](SECURITY.md) before pointing anything automated at this.
`fast-flights` can break when Google changes the page, it can be rate
limited, and automated access may violate Google's terms.

## Tests

Offline tests cover schemas, validation, airport resolution, the page parser,
date-window sampling, and a search that never leaves the machine:

```bash
uv run pytest
```

The live check is opt-in. It searches ZRH to BCN across seven days, one
adult, economy, CHF, a few weeks out:

```bash
FLIGHTS_LIVE=1 uv run pytest tests/test_live.py -s
```

### Live check

`FLIGHTS_LIVE=1 uv run pytest tests/test_live.py` on 2026-09-30. One-way
ZRH to BCN, departures 2026-10-26 through 2026-11-01 (seven days, all
searched), 1 adult, economy, CHF. The call wrote nothing to stdout. Every
priced itinerary had `carry_on` and `checked_bag` set to `unknown` and
`fare_family` null. Nothing was filled in.

Cheapest fare Google returned for each day:

| Departure | Cheapest |
| --- | --- |
| 2026-10-26 | 63 CHF |
| 2026-10-27 | 48 CHF |
| 2026-10-28 | 57 CHF |
| 2026-10-29 | 63 CHF |
| 2026-10-30 | 69 CHF |
| 2026-10-31 | 57 CHF |
| 2026-11-01 | 54 CHF |

The eight itineraries the tool returned (`max_results` 8), sorted by price.
`picks` were the 48 CHF Vueling, the 54 CHF Vueling, and the 75 CHF SWISS.

| Price | Date | Flight | Local times | Duration | Stops | Codeshare |
| --- | --- | --- | --- | --- | --- | --- |
| 48 CHF | 27 Oct | VY1229 | 21:15 → 23:10 | 1h 55m | 0 | IB5489 |
| 54 CHF | 1 Nov | VY1227 | 09:40 → 11:30 | 1h 50m | 0 | IB5487 |
| 57 CHF | 27 Oct | VY1227 | 09:40 → 11:30 | 1h 50m | 0 | IB5487 |
| 57 CHF | 28 Oct | VY1227 | 09:40 → 11:30 | 1h 50m | 0 | IB5487 |
| 57 CHF | 28 Oct | VY1229 | 21:35 → 23:30 | 1h 55m | 0 | IB5489 |
| 57 CHF | 31 Oct | VY1229 | 21:15 → 23:10 | 1h 55m | 0 | IB5489 |
| 63 CHF | 26 Oct | VY1227 | 09:40 → 11:30 | 1h 50m | 0 | IB5487 |
| 75 CHF | 1 Nov | LX1954 | 12:20 → 14:05 | 1h 45m | 0 | none |

Duration basis on these was `google`. The cheapest Vueling was an Airbus
A320, with the carbon figure Google sent (83000 grams) and a booking URL.
Fetching that booking URL from this environment returned HTTP 200 and the
Google Flights page. It is not a held ticket. The booking token is not
copied here.

The same parser, on the 2026-10-28 page alone, also saw connections. They
did not make the shortlist: the nonstops above were cheaper, and the SWISS
flight was faster, so the per-day keep (cheapest plus fastest) never held
onto them. They are recorded because the layover fields were present:

| Price | Flights | Duration | Layover |
| --- | --- | --- | --- |
| 77 CHF | JU331, JU580 | 23h 40m | Belgrade (BEG), 1155 minutes, as Google reported |
| 101 CHF | AF1815, AF1448 | 4h 5m | Paris (CDG), 45 minutes |
| 138 CHF | SN2732, SN3703 | 4h 25m | Brussels (BRU), 70 minutes |

Bags and fare family were absent on those connections too.

Search URL for the first day of the window:

<https://www.google.com/travel/flights/search?tfs=GhoSCjIwMjYtMTAtMjZqBRIDWlJIcgUSA0JDTkIBAUgBmAEC&hl=en-US&curr=CHF>

Treat the tables as a snapshot from that run.

## Layout

- `flights_mcp/server.py` — MCP tools and stdio entrypoint
- `flights_mcp/search.py` — date window, labels, timeout, cache
- `flights_mcp/google_parse.py` — flight numbers, layovers, and prices from the page
- `flights_mcp/dates.py` — the 7-date cap
- `flights_mcp/upstream.py` — one bounded Google Flights fetch
- `flights_mcp/airports.py` — local IATA lookup
- `flights_mcp/models.py` — strict input and output schemas
