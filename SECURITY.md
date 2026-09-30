# Security

This server searches public Google Flights pages through
[fast-flights](https://github.com/AWeirdDev/flights), an unofficial client.
There is no API key and no account. That is a constraint, not a free pass.

## Unofficial Google Flights dependency

`fast-flights` encodes a search as the same protobuf payload the Flights
website uses, then fetches the public page with `primp`, which impersonates a
browser TLS fingerprint. Google does not support this. Expect three kinds of
failure:

- **Breakage.** A change to Google's page or protobuf can make results
  disappear or fail to parse. The server reports that as an error. It does not
  fill in a price.
- **Rate limits and blocks.** Repeated searches from one IP can be throttled
  or challenged. Keep it interactive. The in-process cache (2 minutes) exists
  so a repeated identical question does not hit Google again immediately.
- **Terms of service.** Automated access may violate Google's terms. This
  tool is for a person running it locally. Do not build a fare reseller,
  a polling loop, or a hosted proxy on top of it.

`primp` is pinned next to `fast-flights` so the browser fingerprint does not
drift on a casual reinstall. See `uv.lock`.

## What this process will not do

- No secrets are read from the environment or the repo. None are required.
- User input is not passed to a shell, a subprocess, or a URL path.
- Place names are length-capped and control characters are rejected. Dates
  must be real calendar days from today (Europe/Zurich) through 330 days out.
  A date window searches at most 7 departure dates, sequentially, inside one
  25 second budget. Adults are 1–9. Results are capped at 20.
- Flight numbers and layover minutes are copied from the page payload when
  those slots are present. Carry-on, checked bag, and fare family are not in
  that payload, so the server reports them as unknown. It does not infer them
  from legroom, numeric flags, or the fare amount.
- Response bodies are parsed and discarded. Logs are one line: route, how
  many dates, result count, elapsed time, cache hit or miss, or an error
  code. They do not include the page, the fare list, or a booking token.
- The airport database is the local `airportsdata` snapshot. Resolution does
  not open a socket.
- The fare cache lives in the process only. It is not written to disk.

If you replace `fast-flights`, keep the same rule: live public fares, no paid
account, no API key.
