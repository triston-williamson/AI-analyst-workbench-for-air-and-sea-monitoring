"""Small operational commands used by scripts/pipeline.ps1.

python -m watchfloor.ops health     # data freshness from Snowflake
python -m watchfloor.ops suspend    # suspend the Snowflake warehouse (stop credit use)
"""

from __future__ import annotations

import logging
import sys

from watchfloor.logsetup import NOISY


def health() -> None:
    from watchfloor.dashboard import data

    h = data.health()
    rate = (h["ROWS_5MIN"] or 0) / 5
    print(
        f"  vessels (10 min): {int(h['VESSELS'] or 0):,}   aircraft: {int(h['AIRCRAFT'] or 0):,}"
        f"   msgs/min: {rate:,.0f}"
    )
    print(
        f"  newest load: {h['FRESHNESS_S'] or 0:.0f}s ago   sensor->DB: {h['LATENCY_S'] or 0:.0f}s"
        f"   detector: {(h['DETECTOR_AGE_S'] or 0) / 60:.0f} min ago"
        f"   open alerts: {int(h['OPEN_ALERTS'] or 0)}"
    )


def suspend() -> None:
    from watchfloor.load.snowflake_conn import connect

    conn = connect(warehouse=None)
    try:
        conn.cursor().execute("ALTER WAREHOUSE WATCHFLOOR_WH SUSPEND")
    finally:
        conn.close()
    print("  Snowflake warehouse suspended")


def main() -> None:
    for name, level in NOISY.items():
        logging.getLogger(name).setLevel(level)
    commands = {"health": health, "suspend": suspend}
    if len(sys.argv) != 2 or sys.argv[1] not in commands:
        raise SystemExit(f"usage: python -m watchfloor.ops {{{'|'.join(commands)}}}")
    commands[sys.argv[1]]()


if __name__ == "__main__":
    main()
