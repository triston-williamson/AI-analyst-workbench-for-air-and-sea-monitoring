"""Consume validated track points from Kafka and micro-batch them into Snowflake RAW tables.

Delivery semantics: at-least-once. Offsets are committed only after a batch lands in Snowflake,
so a crash replays the batch. Duplicates are harmless because RECORD_ID is deterministic and
downstream views can dedupe on it.

Production alternative: the Snowflake Kafka Connector with Snowpipe Streaming. This hand-rolled
sink keeps the project self-contained and makes validation/lineage explicit (see docs/DESIGN.md).
"""

from __future__ import annotations

import json
import logging
import time

import pandas as pd
from kafka import KafkaConsumer
from snowflake.connector.pandas_tools import write_pandas

from watchfloor.config import (
    TOPIC_ADSB,
    TOPIC_AIS,
    TOPIC_AIS_STATIC,
    TOPIC_DEAD_LETTER,
    get_settings,
)
from watchfloor.load.snowflake_conn import connect

log = logging.getLogger("sink")

# topic -> (schema, table)
ROUTES = {
    TOPIC_ADSB: ("RAW", "AIRCRAFT_POSITIONS"),
    TOPIC_AIS: ("RAW", "VESSEL_POSITIONS"),
    TOPIC_AIS_STATIC: ("RAW", "VESSEL_STATIC"),
    TOPIC_DEAD_LETTER: ("OPS", "REJECTED_RECORDS"),
}
BATCH_SIZE = 2000
BATCH_SECONDS = 15


def to_row(msg) -> dict:
    row = dict(msg.value)
    if msg.topic == TOPIC_DEAD_LETTER:
        row["errors"] = json.dumps(row.get("errors"))
        return {k.upper(): v for k, v in row.items()}
    row.update(kafka_topic=msg.topic, kafka_partition=msg.partition, kafka_offset=msg.offset)
    return {k.upper(): v for k, v in row.items()}


def flush(conn, buffers: dict[str, list[dict]]) -> int:
    total = 0
    for topic, rows in buffers.items():
        if not rows:
            continue
        schema, table = ROUTES[topic]
        df = pd.DataFrame(rows)
        if schema == "RAW":
            df["LOADED_AT"] = pd.Timestamp.now(tz="UTC")
        for col in ("OBSERVED_AT", "INGESTED_AT", "REJECTED_AT", "LOADED_AT"):
            if col in df:
                df[col] = pd.to_datetime(df[col], utc=True)
        ok, _, nrows, _ = write_pandas(
            conn,
            df,
            table_name=table,
            schema=schema,
            use_logical_type=True,
            quote_identifiers=False,
        )
        if not ok:
            raise RuntimeError(f"write_pandas failed for {schema}.{table}")
        total += nrows
        rows.clear()
    return total


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    cfg = get_settings()
    consumer = KafkaConsumer(
        *ROUTES,
        bootstrap_servers=cfg.kafka_bootstrap,
        group_id="watchfloor-snowflake-sink",
        enable_auto_commit=False,
        auto_offset_reset="earliest",
        value_deserializer=lambda v: json.loads(v.decode()),
    )
    conn = connect()
    buffers: dict[str, list[dict]] = {t: [] for t in ROUTES}
    last_flush = time.monotonic()
    log.info("Sink running: %s -> Snowflake %s", list(ROUTES), cfg.snowflake_database)
    try:
        while True:
            for msgs in consumer.poll(timeout_ms=1000).values():
                for msg in msgs:
                    buffers[msg.topic].append(to_row(msg))
            pending = sum(len(b) for b in buffers.values())
            if pending >= BATCH_SIZE or (pending and time.monotonic() - last_flush > BATCH_SECONDS):
                n = flush(conn, buffers)
                consumer.commit()
                last_flush = time.monotonic()
                log.info("Loaded %d rows", n)
    finally:
        consumer.close()
        conn.close()


if __name__ == "__main__":
    main()
