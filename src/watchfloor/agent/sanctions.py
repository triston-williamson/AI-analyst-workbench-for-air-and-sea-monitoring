"""OFAC SDN (Specially Designated Nationals) screening for vessels.

The SDN list includes sanctioned vessels with their IMO number, MMSI, call sign and flag in the
entry's remarks. ``scripts/fetch_sanctions.py`` downloads the public list from the US Treasury
and stores just the vessel entries locally; this module matches against that copy.

Match strength matters for an analyst, so results are graded:
  * IMO match   - strong: IMO numbers are permanent hull identifiers
  * MMSI match  - medium: MMSIs change when a ship is re-flagged
  * name match  - weak: names are reused and spoofed; flagged for verification only
"""

from __future__ import annotations

import csv
import json
import re
from dataclasses import asdict, dataclass
from functools import lru_cache
from pathlib import Path

DEFAULT_PATH = Path("data") / "ofac" / "sdn_vessels.json"
IMO_RE = re.compile(r"IMO\s*(\d{7})")
MMSI_RE = re.compile(r"MMSI\s*(\d{9})")


@dataclass(frozen=True)
class SdnVessel:
    ent_num: str
    name: str
    program: str
    call_sign: str
    flag: str
    imo: str | None
    mmsi: str | None


def parse_sdn_csv(path: Path) -> list[SdnVessel]:
    """Vessel rows from Treasury's ``sdn.csv`` (no header; ``-0-`` means empty)."""
    out = []
    with path.open(encoding="utf-8", errors="replace", newline="") as f:
        for row in csv.reader(f):
            if len(row) < 12 or row[2].strip().lower() != "vessel":
                continue

            def clean(v: str) -> str:
                v = v.strip()
                return "" if v == "-0-" else v

            remarks = clean(row[11])
            imo = IMO_RE.search(remarks)
            mmsi = MMSI_RE.search(remarks)
            out.append(
                SdnVessel(
                    ent_num=clean(row[0]),
                    name=clean(row[1]),
                    # "UKRAINE-EO13662] [RUSSIA-EO14024" -> "UKRAINE-EO13662, RUSSIA-EO14024"
                    program=", ".join(
                        x.strip(" []")
                        for x in re.split(r"\]\s*\[", clean(row[3]))
                        if x.strip(" []")
                    ),
                    call_sign=clean(row[5]),
                    flag=clean(row[9]),
                    imo=imo.group(1) if imo else None,
                    mmsi=mmsi.group(1) if mmsi else None,
                )
            )
    return out


def save(vessels: list[SdnVessel], path: Path = DEFAULT_PATH, source: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"source": source, "vessels": [asdict(v) for v in vessels]}), encoding="utf-8"
    )


@lru_cache(maxsize=4)
def load(path: str = str(DEFAULT_PATH)) -> tuple[SdnVessel, ...] | None:
    p = Path(path)
    if not p.exists():
        return None
    data = json.loads(p.read_text(encoding="utf-8"))
    return tuple(SdnVessel(**v) for v in data["vessels"])


def _norm(name: str) -> str:
    name = re.sub(r"[^A-Z0-9 ]", " ", name.upper())
    return re.sub(r"\s+", " ", re.sub(r"^(MV|MT|M V|M T)\s+", "", name.strip())).strip()


def screen(
    imo: str | int | None = None,
    mmsi: str | int | None = None,
    name: str | None = None,
    vessels: tuple[SdnVessel, ...] | None = None,
) -> dict:
    """Screen one vessel. Returns {"available": bool, "matches": [...]} for the agent."""
    vessels = load() if vessels is None else vessels
    if vessels is None:
        return {
            "available": False,
            "note": "OFAC SDN list not downloaded; sanctions status could not be checked.",
            "matches": [],
        }
    imo_s = str(imo) if imo else None
    mmsi_s = str(mmsi) if mmsi else None
    name_n = _norm(name) if name else None
    by_imo, by_mmsi, by_name = _index(vessels)
    hits: dict[str, tuple[SdnVessel, str, str]] = {}  # strongest reason per entry
    for v in by_name.get(name_n, []) if name_n and len(name_n) >= 4 else []:
        hits[v.ent_num] = (v, "name only (verify: names are reused and spoofed)", "weak")
    for v in by_mmsi.get(mmsi_s, []) if mmsi_s else []:
        hits[v.ent_num] = (v, "MMSI", "medium")
    for v in by_imo.get(imo_s, []) if imo_s else []:
        hits[v.ent_num] = (v, "IMO number", "strong")
    matches = []
    for v, reason, strength in hits.values():
        matches.append(
            {
                "citation": f"[sdn:{v.ent_num}]",
                "sdn_name": v.name,
                "program": v.program,
                "flag": v.flag,
                "imo": v.imo,
                "mmsi": v.mmsi,
                "matched_on": reason,
                "strength": strength,
            }
        )
    order = {"strong": 0, "medium": 1, "weak": 2}
    matches.sort(key=lambda m: order[m["strength"]])
    return {"available": True, "vessels_on_list": len(vessels), "matches": matches}


_INDEXES: dict[int, tuple[dict, dict, dict]] = {}


def _index(vessels: tuple[SdnVessel, ...]) -> tuple[dict, dict, dict]:
    """IMO / MMSI / normalised-name lookups, built once per loaded list. A linear scan of
    ~1,600 entries for each of ~3,300 vessels every detector pass would be millions of
    comparisons; the index makes each lookup O(1)."""
    key = id(vessels)
    if key not in _INDEXES:
        by_imo: dict[str, list] = {}
        by_mmsi: dict[str, list] = {}
        by_name: dict[str, list] = {}
        for v in vessels:
            if v.imo:
                by_imo.setdefault(v.imo, []).append(v)
            if v.mmsi:
                by_mmsi.setdefault(v.mmsi, []).append(v)
            by_name.setdefault(_norm(v.name), []).append(v)
        _INDEXES.clear()  # keep only the current list
        _INDEXES[key] = (by_imo, by_mmsi, by_name)
    return _INDEXES[key]
