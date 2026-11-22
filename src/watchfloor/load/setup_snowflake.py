"""Run sql/001_setup.sql against your Snowflake account.

Connects as SNOWFLAKE_USER with role ACCOUNTADMIN (needed once to create the warehouse and role);
the script then switches to WATCHFLOOR_ROLE for everything it creates.

    python -m watchfloor.load.setup_snowflake
"""

from __future__ import annotations

from importlib.resources import files

from watchfloor.config import get_settings
from watchfloor.load.snowflake_conn import connect


def main() -> None:
    cfg = get_settings()
    sql = (files("watchfloor") / "sql" / "001_setup.sql").read_text()
    sql = sql.replace("<YOUR_USER>", f'"{cfg.snowflake_user.upper()}"')
    conn = connect(role="ACCOUNTADMIN", warehouse=None, database=None, schema=None)
    try:
        for cur in conn.execute_string(sql, remove_comments=True):
            print(f"ok: {cur.query.strip().splitlines()[0][:80]}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
