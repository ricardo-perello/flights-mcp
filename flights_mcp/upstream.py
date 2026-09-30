"""One bounded request to public Google Flights pages.

fast-flights builds the query. This module fetches the page and drops the
body after parsing. Nothing is logged here.
"""

from __future__ import annotations

from fast_flights.exceptions import FlightsNotFound
from fast_flights.querying import Query

from flights_mcp.google_parse import ParsedItinerary, parse_html

_SEARCH_URL = "https://www.google.com/travel/flights"
_MAX_BODY_CHARS = 5_000_000
# Same browser profile fast-flights 3.1 uses. Pinned so a primp upgrade cannot
# silently change the fingerprint.
_IMPERSONATE = "chrome_145"
_IMPERSONATE_OS = "macos"


class UpstreamFailure(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _timeout_error(exc: BaseException) -> bool:
    return isinstance(exc, TimeoutError) or "timeout" in type(exc).__name__.lower()


def fetch_results(query: Query, *, timeout_seconds: float) -> list[ParsedItinerary]:
    """Return parsed itineraries, or raise FlightsNotFound / UpstreamFailure.

    The response body is parsed and dropped. It is not logged and not attached
    to the exception.
    """
    from primp import Client

    timeout = float(timeout_seconds)
    client = Client(
        impersonate=_IMPERSONATE,
        impersonate_os=_IMPERSONATE_OS,
        referer=True,
        cookie_store=True,
        timeout=timeout,
    )
    try:
        response = client.get(_SEARCH_URL, params=query.params(), timeout=timeout)
    except FlightsNotFound:
        raise
    except Exception as exc:
        if _timeout_error(exc):
            raise UpstreamFailure("timeout", "Google Flights did not answer before the timeout.") from exc
        raise UpstreamFailure(
            "upstream",
            f"Google Flights request failed ({type(exc).__name__}).",
        ) from exc

    status = getattr(response, "status_code", None)
    if status != 200:
        raise UpstreamFailure("upstream", f"Google Flights returned HTTP {status}.")

    try:
        body = response.text
    except Exception as exc:
        raise UpstreamFailure("upstream", "Google Flights returned an unreadable response.") from exc

    if not isinstance(body, str) or not body:
        raise UpstreamFailure("upstream", "Google Flights returned an empty page.")
    if len(body) > _MAX_BODY_CHARS:
        raise UpstreamFailure("upstream", "Google Flights response exceeded the size limit.")

    try:
        parsed = parse_html(body)
    except FlightsNotFound:
        raise
    except Exception as exc:
        raise UpstreamFailure("upstream", "Could not read flight results from Google Flights.") from exc
    finally:
        del body
    return parsed
