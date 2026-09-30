"""Shared fixtures for offline tests. No network."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from flights_mcp.google_parse import ParsedItinerary, ParsedLayover, ParsedLeg

ZURICH = ZoneInfo("Europe/Zurich")


def depart_in(days: int) -> str:
    return (datetime.now(ZURICH).date() + timedelta(days=days)).isoformat()


def _leg(
    day: str,
    origin: str,
    origin_name: str,
    destination: str,
    destination_name: str,
    depart: str,
    arrive: str,
    minutes: int,
    flight_number: str | None,
    airline_code: str | None,
    airline_name: str | None,
    aircraft: str,
) -> ParsedLeg:
    return ParsedLeg(
        origin=origin,
        origin_name=origin_name,
        destination=destination,
        destination_name=destination_name,
        depart_local=f"{day}T{depart}",
        arrive_local=f"{day}T{arrive}",
        duration_minutes=minutes,
        aircraft=aircraft,
        flight_number=flight_number,
        airline_code=airline_code,
        airline_name=airline_name,
        codeshare_flight_number=None,
    )


def zurich_barcelona(
    day: str,
    *,
    price: int | None = 108,
    stops: bool = False,
    duration_minutes: int | None = None,
    flight_number: str | None = None,
    layover_minutes: int | None = 355,
) -> ParsedItinerary:
    """A small itinerary shaped like a parsed Google result. Not a live fare."""
    if stops:
        numbers = ["JU331", "JU580"] if flight_number is None else [flight_number, "JU580"]
        legs = [
            _leg(
                day,
                "ZRH",
                "Zurich Airport",
                "BEG",
                "Belgrade Nikola Tesla Airport",
                "09:30",
                "11:15",
                105,
                numbers[0],
                "JU",
                "Air Serbia",
                "Embraer 195",
            ),
            _leg(
                day,
                "BEG",
                "Belgrade Nikola Tesla Airport",
                "BCN",
                "Josep Tarradellas Barcelona-El Prat Airport",
                "17:10",
                "19:50",
                160,
                numbers[1],
                "JU",
                "Air Serbia",
                "Airbus A220-300 Passenger",
            ),
        ]
        layovers = [
            ParsedLayover(
                airport="BEG",
                airport_name="Belgrade Nikola Tesla Airport",
                city="Belgrade",
                minutes=layover_minutes,
            )
        ]
        return ParsedItinerary(
            price=price,
            airlines=["Air Serbia"],
            airline_code="JU",
            legs=legs,
            layovers=layovers,
            duration_minutes=duration_minutes,
            carbon_emission_grams=270000,
            booking_token="CjRItesttoken",
        )
    number = "LX1956" if flight_number is None else flight_number
    return ParsedItinerary(
        price=price,
        airlines=["SWISS"],
        airline_code="LX",
        legs=[
            _leg(
                day,
                "ZRH",
                "Zurich Airport",
                "BCN",
                "Josep Tarradellas Barcelona-El Prat Airport",
                "09:30",
                "11:10",
                100,
                number,
                "LX" if number else None,
                "SWISS" if number else None,
                "Airbus A320neo",
            )
        ],
        layovers=[],
        duration_minutes=100 if duration_minutes is None else duration_minutes,
        carbon_emission_grams=76000,
        booking_token="CjRItesttoken",
    )
