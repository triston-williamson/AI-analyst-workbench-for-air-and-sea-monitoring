"""Port exclusion zones.

Vessels sitting in port, or crawling through a harbour, look exactly like "loitering" or
"rendezvous" to a naive detector. These circles suppress those false positives.

Coordinates are approximate harbour centres with a generous radius. This list is a backstop:
the main defences against harbour false positives are data-driven (AIS "moored" status and the
rendezvous crowd filter), which work even at ports missing from here. A production system would
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
    # Added after the first live run flooded RENDEZVOUS alerts at these berths.
    Port("Hamburg", 53.54, 9.95, 8.0),
    Port("Hanko", 59.82, 22.97),
    Port("Fredrikstad", 59.21, 10.94),
    Port("Moss", 59.43, 10.66),
    Port("Horten", 59.42, 10.48),
    Port("Tonsberg", 59.27, 10.41),
    Port("Sandefjord", 59.13, 10.23),
    Port("Larvik", 59.05, 10.03),
    Port("Drammen", 59.74, 10.21),
    Port("Halden", 59.12, 11.39),
    Port("Lysekil", 58.27, 11.43),
    Port("Stenungsund", 58.07, 11.82),
    Port("Uddevalla", 58.35, 11.93),
    Port("Varberg", 57.10, 12.25),
    Port("Halmstad", 56.66, 12.85),
    Port("Aalborg", 57.05, 9.93, 5.0),
    Port("Grenaa", 56.41, 10.92),
    Port("Fredericia", 55.56, 9.75),
    Port("Odense", 55.42, 10.39),
    Port("Nyborg-Korsor", 55.32, 10.95, 6.0),
    Port("Kalundborg", 55.68, 11.08),
    Port("Puttgarden-Rodby", 54.57, 11.29, 6.0),
    Port("Gedser", 54.57, 11.93),
    Port("Wismar", 53.90, 11.46),
    Port("Stralsund", 54.31, 13.10),
    Port("Sassnitz-Mukran", 54.48, 13.60),
    Port("Ystad", 55.42, 13.82),
    Port("Ronne", 55.10, 14.69),
    Port("Kolobrzeg", 54.18, 15.57),
    Port("Kalmar", 56.66, 16.37),
    Port("Oxelosund", 58.67, 17.10),
    Port("Sodertalje", 59.20, 17.63),
    Port("Naantali", 60.47, 22.02),
    Port("Porvoo-Kilpilahti", 60.30, 25.55, 5.0),
    Port("Loviisa", 60.43, 26.27),
    Port("Kunda", 59.52, 26.53),
    Port("Sillamae", 59.40, 27.75),
    Port("Parnu", 58.38, 24.48),
    Port("Kronstadt", 59.99, 29.77, 5.0),
    Port("Kuressaare-Roomassaare", 58.23, 22.50, 4.0),
    # Kiel Canal: ships queue and pass at low speed along its whole length (inside the AOI).
    Port("Kiel Canal (west)", 54.17, 9.52, 6.0),
    Port("Kiel Canal (Rendsburg)", 54.30, 9.67, 6.0),
    Port("Kiel Canal (east)", 54.36, 9.95, 6.0),
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
