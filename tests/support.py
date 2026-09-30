"""Shared fixtures for offline tests. No network."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from fast_flights.model import Airport, CarbonEmission, Flights, SimpleDatetime, SingleFlight

ZURICH = ZoneInfo("Europe/Zurich")


def depart_in(days: int) -> str:
    return (datetime.now(ZURICH).date() + timedelta(days=days)).isoformat()


def _stamp(day: str, hour: int, minute: int) -> SimpleDatetime:
    year, month, dom = (int(part) for part in day.split("-"))
    return SimpleDatetime(date=(year, month, dom), time=(hour, minute))


def zurich_barcelona(day: str, *, price: int | None = 108, stops: bool = False) -> Flights:
    """A small itinerary shaped like the fast-flights model. Not a live fare."""
    outbound = SingleFlight(
        from_airport=Airport(name="Zurich Airport", code="ZRH"),
        to_airport=Airport(name="Belgrade Nikola Tesla Airport", code="BEG")
        if stops
        else Airport(name="Josep Tarradellas Barcelona-El Prat Airport", code="BCN"),
        departure=_stamp(day, 9, 30),
        arrival=_stamp(day, 11, 15) if stops else _stamp(day, 11, 10),
        duration=105 if stops else 100,
        plane_type="Embraer 195" if stops else "Airbus A320neo",
    )
    legs = [outbound]
    if stops:
        legs.append(
            SingleFlight(
                from_airport=Airport(name="Belgrade Nikola Tesla Airport", code="BEG"),
                to_airport=Airport(name="Josep Tarradellas Barcelona-El Prat Airport", code="BCN"),
                departure=_stamp(day, 17, 10),
                arrival=_stamp(day, 19, 50),
                duration=160,
                plane_type="Airbus A220-300 Passenger",
            )
        )
    return Flights(
        type="JU" if stops else "LX",
        price=price,
        airlines=["Air Serbia"] if stops else ["SWISS"],
        flights=legs,
        carbon=CarbonEmission(typical_on_route=200000, emission=270000 if stops else 76000),
    )
