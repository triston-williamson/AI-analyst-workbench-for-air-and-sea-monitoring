"""Central configuration, loaded from environment variables / .env."""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# Kafka topic names - one place so producers and consumers can't drift.
TOPIC_ADSB = "adsb.positions"
TOPIC_AIS = "ais.positions"
TOPIC_AIS_STATIC = "ais.static"
TOPIC_DEAD_LETTER = "ingest.dead_letter"


class Settings(BaseSettings):
    """Secrets use SecretStr so they print as '**********' in logs, tracebacks and reprs."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    kafka_bootstrap: str = "localhost:9092"

    # NoDecode: parse "a,b,c,d" ourselves instead of pydantic-settings expecting JSON.
    aoi_bbox: Annotated[tuple[float, float, float, float], NoDecode] = Field(
        default=(53.5, 9.5, 60.5, 30.0)
    )

    opensky_client_id: str = ""
    opensky_client_secret: SecretStr = SecretStr("")
    adsb_poll_seconds: int = 30

    aisstream_api_key: SecretStr = SecretStr("")

    snowflake_account: str = ""
    snowflake_user: str = ""
    snowflake_password: SecretStr = SecretStr("")
    snowflake_private_key_file: str = ""
    snowflake_role: str = "WATCHFLOOR_ROLE"
    snowflake_warehouse: str = "WATCHFLOOR_WH"
    snowflake_database: str = "WATCHFLOOR"
    snowflake_schema: str = "RAW"

    audit_log_path: str = "logs/audit.jsonl"
    # Public demo: a snapshot folder from scripts/export_demo_snapshot.py. When set, the console
    # reads it instead of Snowflake and is read-only (no review, no AI drafting).
    demo_dir: str = ""
    # Name recorded in OPS.ALERTS and the audit log when approving/rejecting in the console.
    analyst_name: str = "analyst"

    # --- Phase 3: AI analyst agent (Claude API) ---
    # Optional here: the SDK also finds ANTHROPIC_API_KEY in the environment or an
    # `ant auth login` profile.
    anthropic_api_key: SecretStr = SecretStr("")
    agent_model: str = "claude-opus-5-5"
    agent_effort: str = "high"  # low | medium | high | xhigh | max

    @field_validator("aoi_bbox", mode="before")
    @classmethod
    def _parse_bbox(cls, v):
        if isinstance(v, str):
            parts = [float(p) for p in v.split(",")]
            if len(parts) != 4:
                raise ValueError("AOI_BBOX must be min_lat,min_lon,max_lat,max_lon")
            v = tuple(parts)
        min_lat, min_lon, max_lat, max_lon = v
        if not (-90 <= min_lat < max_lat <= 90 and -180 <= min_lon < max_lon <= 180):
            raise ValueError("AOI_BBOX is out of range or inverted")
        return v


@lru_cache
def get_settings() -> Settings:
    return Settings()
