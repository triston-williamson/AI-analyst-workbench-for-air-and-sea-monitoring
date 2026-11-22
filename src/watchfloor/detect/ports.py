"""Port exclusion zones.

Vessels sitting in port, or crawling through a harbour, look exactly like "loitering" or
"rendezvous" to a naive detector. These circles suppress those false positives.

Coordinates are approximate harbour centres with a generous radius. A production system would
load the full NGA World Port Index (public) or harbour polygons instead of this hand-curated list.
"""

from __future__ import annotations

from dataclasses import dataclass

from watchfloor.detect.geo import haversine_nm


@dataclass(frozen=True)
class Port:
    name: str
    lat: float
    lon: float
    radius_nm: float = 4.0


# Major Baltic / Kattegat / Skagerrak ports inside the default AOI.
BALTIC_PORTS: tuple[Port, ...] = (
    Port("Aarhus", 56.15, 10.23),
    Port("Frederikshavn", 57.44, 10.55),
    Port("Gothenburg", 57.69, 11.87, 6.0),
    Port("Oslo", 59.90, 10.73, 6.0),
    Port("Kiel", 54.36, 10.16, 5.0),
    Port("Lubeck-Travemunde", 53.95, 10.87, 5.0),
    Port("Rostock", 54.15, 12.10),
    Port("Copenhagen", 55.69, 12.60, 5.0),
    Port("Malmo", 55.62, 12.99),
    Port("Helsingborg-Helsingor", 56.04, 12.66),
    Port("Trelleborg", 55.37, 13.15),
    Port("Szczecin-Swinoujscie", 53.91, 14.25, 6.0),
    Port("Karlskrona", 56.16, 15.59),
    Port("Gdansk", 54.40, 18.67, 5.0),
    Port("Gdynia", 54.53, 18.56),
    Port("Baltiysk-Kaliningrad", 54.65, 19.90, 6.0),
    Port("Klaipeda", 55.71, 21.12),
    Port("Liepaja", 56.52, 21.00),
    Port("Ventspils", 57.40, 21.55),
    Port("Riga", 57.03, 24.08, 6.0),
    Port("Visby", 57.64, 18.29),
    Port("Nynashamn", 58.90, 17.95),
    Port("Stockholm", 59.33, 18.10, 6.0),
    Port("Mariehamn", 60.10, 19.94),
    Port("Turku", 60.43, 22.22, 5.0),
    Port("Paldiski", 59.35, 24.05),
    Port("Tallinn-Muuga", 59.47, 24.85, 6.0),
    Port("Helsinki-Vuosaari", 60.18, 25.05, 6.0),
    Port("Kotka-Hamina", 60.50, 27.05, 6.0),
    Port("Primorsk", 60.35, 28.67, 5.0),
    Port("Ust-Luga", 59.68, 28.40, 5.0),
)


def port_at(lat: float, lon: float, ports: tuple[Port, ...] = BALTIC_PORTS) -> Port | None:
    """Return the port whose zone contains the position, if any."""
    for p in ports:
        # Cheap bounding check first (1 deg lat = 60 nm), then exact distance.
        if abs(lat - p.lat) * 60 > p.radius_nm:
            continue
        if haversine_nm(lat, lon, p.lat, p.lon) <= p.radius_nm:
            return p
    return None


def in_port(lat: float, lon: float) -> bool:
    return port_at(lat, lon) is not None
