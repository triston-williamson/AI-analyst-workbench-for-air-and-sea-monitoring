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
    matches = []
    for v in vessels:
        reason = None
        if imo_s and v.imo == imo_s:
            reason, strength = "IMO number", "strong"
        elif mmsi_s and v.mmsi == mmsi_s:
            reason, strength = "MMSI", "medium"
        elif name_n and len(name_n) >= 4 and _norm(v.name) == name_n:
            reason, strength = "name only (verify: names are reused and spoofed)", "weak"
        if reason:
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
    return {"available": True, "vessels_on_list": len(vessels), "matches": matches}
