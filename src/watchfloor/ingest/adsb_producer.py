"""Poll OpenSky Network for live ADS-B state vectors inside the AOI and publish to Kafka.

OpenSky API docs: https://openskynetwork.github.io/opensky-api/rest.html
Anonymous access works but is rate-limited; set OPENSKY_CLIENT_ID/SECRET for more credits.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import UTC, datetime

import httpx

from watchfloor.config import TOPIC_ADSB, get_settings
from watchfloor.ingest.kafka_utils import Publisher, make_producer
from watchfloor.logsetup import configure as configure_logging
from watchfloor.models import AircraftPosition, make_record_id

log = logging.getLogger("adsb")

STATES_URL = "https://opensky-network.org/api/states/all"
# OAuth endpoint URL, not a secret.
TOKEN_URL = (
    "https://auth.opensky-network.org/auth/realms/opensky-network/protocol/openid-connect/token"  # noqa: S105
)
MS_TO_KTS = 1.943844


class OpenSkyClient:
    def __init__(self, client_id: str, client_secret: str):
        self.client_id = client_id
        self.client_secret = client_secret
        self._token: str | None = None
        self._token_expires = 0.0
        self.http = httpx.Client(timeout=30)

    def _headers(self) -> dict[str, str]:
        if not self.client_id:
            return {}
        if time.time() > self._token_expires - 60:
            resp = self.http.post(
                TOKEN_URL,
                data={
                    "grant_type": "client_credentials",
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                },
            )
            resp.raise_for_status()
            body = resp.json()
            self._token = body["access_token"]
            self._token_expires = time.time() + body.get("expires_in", 1800)
        return {"Authorization": f"Bearer {self._token}"}

    def states(self, bbox: tuple[float, float, float, float]) -> list[list]:
        min_lat, min_lon, max_lat, max_lon = bbox
        resp = self.http.get(
            STATES_URL,
            params={"lamin": min_lat, "lomin": min_lon, "lamax": max_lat, "lomax": max_lon},
            headers=self._headers(),
        )
        resp.raise_for_status()
        return resp.json().get("states") or []


def state_to_record(s: list) -> dict:
    """Map an OpenSky state vector (positional array) to our AircraftPosition fields."""
    raw = json.dumps(s)
    ts = s[3] or s[4]  # time_position, else last_contact
    return {
        "record_id": make_record_id("opensky", raw),
        "observed_at": datetime.fromtimestamp(ts, UTC) if ts else None,
        "icao24": (s[0] or "").lower(),
        "callsign": (s[1] or "").strip() or None,
        "origin_country": s[2],
        "lon": s[5],
        "lat": s[6],
        "baro_altitude_m": s[7],
        "on_ground": bool(s[8]),
        "speed_kts": s[9] * MS_TO_KTS if s[9] is not None else None,
        "course_deg": (s[10] % 360) if s[10] is not None else None,
        "squawk": s[14],
        "raw_payload": raw,
    }


def main() -> None:
    configure_logging("adsb_producer")
    cfg = get_settings()
    client = OpenSkyClient(cfg.opensky_client_id, cfg.opensky_client_secret.get_secret_value())
    pub = Publisher(make_producer(), TOPIC_ADSB)
    log.info("Polling OpenSky every %ss for bbox %s", cfg.adsb_poll_seconds, cfg.aoi_bbox)
    while True:
        try:
            for s in client.states(cfg.aoi_bbox):
                pub.publish(AircraftPosition, state_to_record(s), key=(s[0] or "").lower())
            pub.flush_and_report()
        except httpx.HTTPStatusError as exc:
            log.warning("OpenSky HTTP %s - backing off", exc.response.status_code)
            time.sleep(60)
        except httpx.HTTPError as exc:
            log.warning("OpenSky request failed: %s", exc)
        time.sleep(cfg.adsb_poll_seconds)


if __name__ == "__main__":
    main()
