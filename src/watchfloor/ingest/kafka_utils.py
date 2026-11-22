"""Shared Kafka helpers: JSON producer and dead-letter publishing."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

from kafka import KafkaProducer
from pydantic import BaseModel, ValidationError

from watchfloor.config import TOPIC_DEAD_LETTER, get_settings

log = logging.getLogger(__name__)


def make_producer() -> KafkaProducer:
    return KafkaProducer(
        bootstrap_servers=get_settings().kafka_bootstrap,
        key_serializer=lambda k: k.encode() if k else None,
        value_serializer=lambda v: json.dumps(v, default=str).encode(),
        acks="all",
        linger_ms=50,
    )


class Publisher:
    """Validates records and routes them to the clean topic or the dead-letter topic."""

    def __init__(self, producer: KafkaProducer, topic: str):
        self.producer = producer
        self.topic = topic
        self.ok = 0
        self.rejected = 0

    def publish(self, model: type[BaseModel], data: dict, key: str | None) -> None:
        try:
            record = model.model_validate(data)
        except ValidationError as exc:
            self.rejected += 1
            self.producer.send(
                TOPIC_DEAD_LETTER,
                key=key,
                value={
                    "target_topic": self.topic,
                    "rejected_at": datetime.now(UTC).isoformat(),
                    "errors": exc.errors(include_url=False, include_input=False),
                    "raw_payload": data.get("raw_payload"),
                },
            )
            return
        self.ok += 1
        self.producer.send(self.topic, key=key, value=record.model_dump(mode="json"))

    def flush_and_report(self) -> None:
        self.producer.flush()
        log.info("%s: published=%d rejected=%d", self.topic, self.ok, self.rejected)
