"""AIS ship type codes (ITU-R M.1371, table 53) and how detectors treat them.

Vessels broadcast a two-digit type code in their static data. It is self-reported (and can be
wrong or spoofed), so it is used to *down-weight* behaviour that is normal for a vessel's job,
never as proof of innocence or guilt.
"""

from __future__ import annotations

_SPECIFIC = {
    30: "Fishing",
    31: "Towing",
    32: "Towing (large)",
    33: "Dredging / underwater ops",
    34: "Diving ops",
    35: "Military ops",
    36: "Sailing",
    37: "Pleasure craft",
    50: "Pilot vessel",
    51: "Search and rescue",
    52: "Tug",
    53: "Port tender",
    54: "Anti-pollution",
    55: "Law enforcement",
    58: "Medical transport",
    59: "Noncombatant (RR18)",
}
_RANGES = (
    (20, 29, "Wing in ground"),
    (40, 49, "High-speed craft"),
    (60, 69, "Passenger"),
    (70, 79, "Cargo"),
    (80, 89, "Tanker"),
    (90, 99, "Other"),
)

# Harbour and working craft whose normal job looks like loitering or meeting other ships:
# pilots wait at stations and board ships, tugs push alongside, SAR and patrol boats hold
# position, fishing vessels pair-trawl, pleasure craft raft up. Excluded from LOITER,
# RENDEZVOUS and ML scoring. Deliberately NOT excluded: 33 dredging/underwater ops and
# 35 military, because those are exactly what undersea-infrastructure monitoring cares about.
SERVICE_TYPES = frozenset({30, 31, 32, 34, 36, 37, 50, 51, 52, 53, 54, 55, 58})

TANKER_TYPES = frozenset(range(80, 90))
CARGO_TYPES = frozenset(range(70, 80))


def label(code: int | None) -> str:
    if code is None or code == 0:
        return "Unknown"
    if code in _SPECIFIC:
        return _SPECIFIC[code]
    for lo, hi, name in _RANGES:
        if lo <= code <= hi:
            return name
    return f"Type {code}"


def is_service(code: int | None) -> bool:
    return code in SERVICE_TYPES


def is_tanker(code: int | None) -> bool:
    return code in TANKER_TYPES


def is_cargo_or_tanker(code: int | None) -> bool:
    return code in TANKER_TYPES or code in CARGO_TYPES
