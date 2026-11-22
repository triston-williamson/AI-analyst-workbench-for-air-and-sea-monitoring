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


MAX_POSITION_AGE_S = 30


class StaleFilter:
    """Drop state vectors whose position is not a new observation.

    OpenSky keeps returning an aircraft's *last known* position (with its old ``time_position``)
    while other fields such as altitude keep updating. Stored as-is, every poll adds a "new"
    record at the same place and time: in the first live day 33% of aircraft rows were such
    repeats, which made a climbing airliner look like an aircraft barely moving and led the
    analyst agent to escalate a false alarm. A position is kept only if it has a timestamp, is
    at most ``MAX_POSITION_AGE_S`` older than the aircraft's last contact, and is newer than the
    last position kept for that aircraft.
    """

    def __init__(self) -> None:
        self.last_kept: dict[str, int] = {}
        self.dropped = {"no_position_time": 0, "stale": 0, "repeat": 0}

    def keep(self, s: list) -> bool:
        icao, t_pos, t_contact = (s[0] or "").lower(), s[3], s[4]
        if t_pos is None:
            self.dropped["no_position_time"] += 1
            return False
        if t_contact is not None and t_contact - t_pos > MAX_POSITION_AGE_S:
            self.dropped["stale"] += 1
            return False
        if t_pos <= self.last_kept.get(icao, -1):
            self.dropped["repeat"] += 1
            return False
        self.last_kept[icao] = t_pos
        return True


def state_to_record(s: list) -> dict:
    """Map an OpenSky state vector (positional array) to our AircraftPosition fields."""
    raw = json.dumps(s)
    ts = s[3]  # time_position: when the *position* was observed (StaleFilter ensures it's set)
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
    stale = StaleFilter()
    log.info("Polling OpenSky every %ss for bbox %s", cfg.adsb_poll_seconds, cfg.aoi_bbox)
    while True:
        try:
            for s in client.states(cfg.aoi_bbox):
                if stale.keep(s):
                    pub.publish(AircraftPosition, state_to_record(s), key=(s[0] or "").lower())
            pub.flush_and_report()
            log.info("stale positions dropped so far: %s", stale.dropped)
        except httpx.HTTPStatusError as exc:
            log.warning("OpenSky HTTP %s - backing off", exc.response.status_code)
            time.sleep(60)
        except httpx.HTTPError as exc:
            log.warning("OpenSky request failed: %s", exc)
        time.sleep(cfg.adsb_poll_seconds)


if __name__ == "__main__":
    main()
