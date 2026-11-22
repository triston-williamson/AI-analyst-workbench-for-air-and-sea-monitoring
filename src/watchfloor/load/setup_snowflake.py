"""Apply every sql/NNN_*.sql file, in order, to your Snowflake account.

All scripts are idempotent, so re-running after pulling new migrations is safe.

Connects as SNOWFLAKE_USER with role ACCOUNTADMIN (needed once to create the warehouse and role);
the script then switches to WATCHFLOOR_ROLE for everything it creates.

    python -m watchfloor.load.setup_snowflake
"""

from __future__ import annotations

from importlib.resources import files

from watchfloor.load.snowflake_conn import connect


def main() -> None:
    scripts = sorted(
        (f for f in (files("watchfloor") / "sql").iterdir() if f.name.endswith(".sql")),
        key=lambda f: f.name,
    )
    conn = connect(role="ACCOUNTADMIN", warehouse=None, database=None, schema=None)
    try:
        for script in scripts:
            print(f"== {script.name}")
            for cur in conn.execute_string(script.read_text(), remove_comments=True):
                print(f"ok: {cur.query.strip().splitlines()[0][:80]}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
