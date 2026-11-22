"""Download the OFAC SDN list from the US Treasury and keep the vessel entries.

    python scripts/fetch_sanctions.py

Source: https://www.treasury.gov/ofac/downloads/sdn.csv (public, US Treasury OFAC). The raw
file goes to data/ofac/sdn.csv; the vessel subset used by the analyst agent goes to
data/ofac/sdn_vessels.json. data/ is git-ignored: re-run this to refresh the list.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from watchfloor.agent.sanctions import DEFAULT_PATH, parse_sdn_csv, save  # noqa: E402

URL = "https://www.treasury.gov/ofac/downloads/sdn.csv"
RAW = Path("data") / "ofac" / "sdn.csv"


def main() -> None:
    RAW.parent.mkdir(parents=True, exist_ok=True)
    with httpx.Client(timeout=120, follow_redirects=True) as http:
        resp = http.get(URL)
        resp.raise_for_status()
    RAW.write_bytes(resp.content)
    vessels = parse_sdn_csv(RAW)
    if not vessels:
        raise SystemExit("No vessel entries parsed: the file format may have changed.")
    stamp = datetime.now(UTC).isoformat(timespec="seconds")
    save(vessels, DEFAULT_PATH, source=f"{URL} downloaded {stamp}")
    with_imo = sum(1 for v in vessels if v.imo)
    print(
        f"downloaded {len(resp.content) / 1e6:.1f} MB; {len(vessels)} sanctioned vessels "
        f"({with_imo} with IMO) -> {DEFAULT_PATH}"
    )


if __name__ == "__main__":
    main()
