"""Snowflake connection factory (password or key-pair auth)."""

from __future__ import annotations

from pathlib import Path

import snowflake.connector
from cryptography.hazmat.primitives import serialization

from watchfloor.config import get_settings


def connect(**overrides) -> snowflake.connector.SnowflakeConnection:
    """Open a connection using .env settings. Pass e.g. role="ACCOUNTADMIN" to override;
    pass database=None to omit a setting entirely."""
    cfg = get_settings()
    if not (cfg.snowflake_account and cfg.snowflake_user):
        raise SystemExit("Set SNOWFLAKE_ACCOUNT and SNOWFLAKE_USER in .env")
    kwargs: dict = {
        "account": cfg.snowflake_account,
        "user": cfg.snowflake_user,
        "role": cfg.snowflake_role,
        "warehouse": cfg.snowflake_warehouse,
        "database": cfg.snowflake_database,
        "schema": cfg.snowflake_schema,
        "application": "watchfloor",
    }
    kwargs.update(overrides)
    kwargs = {k: v for k, v in kwargs.items() if v}
    if cfg.snowflake_private_key_file:
        key = serialization.load_pem_private_key(
            Path(cfg.snowflake_private_key_file).read_bytes(), password=None
        )
        kwargs["private_key"] = key.private_bytes(
            encoding=serialization.Encoding.DER,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    else:
        kwargs["password"] = cfg.snowflake_password.get_secret_value()
    return snowflake.connector.connect(**kwargs)
