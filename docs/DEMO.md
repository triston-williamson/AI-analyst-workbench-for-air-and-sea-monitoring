# Public demo

**Live at <https://watchfloor-demo.onrender.com/>** (deployed from
[triston-williamson/watchfloor-demo](https://github.com/triston-williamson/watchfloor-demo)).

The public demo is the real operator console running on a **read-only snapshot** of collected
data, so anyone can explore it without Snowflake, Kafka, API keys or running costs.

| | Live system | Public demo |
|---|---|---|
| Data | Kafka → Snowflake, refreshed every 30 s | Six hours of real Baltic data exported to Parquet (0254Z–0854Z, 22 Nov 2026) |
| "Now" | The current time | The end of the snapshot (the clock reads `AS OF …`) |
| Map, queue, evidence, replay, heatmaps, audit history | Yes | Yes |
| AI reports | Drafted on demand with Claude | The six reports Claude drafted on this data |
| Approve / Reject, Draft AI report | Yes | Turned off, in the layout **and** in the server callbacks |
| Credentials in the deployment | Snowflake key, Anthropic key | None |
| Cost | About 1 Snowflake credit per hour while running | Free (Render free web service) |

## How it works

- `scripts/export_demo_snapshot.py` exports a time window: vessel and aircraft positions,
  vessel identity, the alerts first raised in the window, their AI reports, the sensor
  records they cite (with raw payloads) and their audit-log entries. Raw payloads are kept
  only for cited evidence. Reviewer names become "analyst". Alerts that later data extended
  past the window are left out (for example AIS "gaps" that span a pipeline outage).
- `src/watchfloor/dashboard/demo_data.py` (`DemoData`) answers the same calls as `data.py`
  with the same columns, from the snapshot, treating its last observation as "now". It cannot
  write: review, SQL and drafting raise `ReadOnlyDemo`.
- Setting `DEMO_DIR` switches the console to it (`app.py`): a PUBLIC DEMO note in the header,
  review controls disabled, the drafting button hidden, and the review and drafting callbacks
  refuse requests even if someone crafts one by hand.
- `deploy/demo/` holds the container (`Dockerfile`: gunicorn as an unprivileged user, on
  `$PORT` or 7860), a Render Blueprint (`render.yaml`) and the bundle's README.
- `scripts/build_demo_bundle.py` assembles `build/demo/`: those files, the package source and
  the snapshot (about 11 MB), with no `.env`, logs or keys.
- `tests/test_demo.py` covers the data layer against a small synthetic snapshot, the read-only
  console and the exporter's audit filtering.

Measured locally: about 260–320 MB of memory in use while browsing, inside Render's free
512 MB.

## Run it locally

```powershell
python scripts/export_demo_snapshot.py --start 2026-11-22T02:45Z --end 2026-11-22T09:00Z
$env:DEMO_DIR = "demo_snapshot"; python -m watchfloor.dashboard.app   # http://127.0.0.1:8050
Remove-Item Env:DEMO_DIR
```

Or in the same container the public demo runs in:

```powershell
python scripts/build_demo_bundle.py
docker build -t watchfloor-demo build/demo
docker run --rm -p 7860:7860 watchfloor-demo                         # http://127.0.0.1:7860
```

## Deploy to Render (free)

The bundle is deployed from its own small public GitHub repository, so the snapshot stays out
of this one.

1. On GitHub, create an empty public repository, e.g. `watchfloor-demo` (no README).
2. Build the bundle and push it there:

   ```powershell
   python scripts/build_demo_bundle.py
   cd build/demo
   git init -b main
   git add -A
   git commit -m "Watchfloor public demo"
   git remote add origin https://github.com/<you>/watchfloor-demo.git
   git push -u origin main
   ```

3. Sign up at [render.com](https://render.com) with GitHub, choose **New → Blueprint**, pick
   the `watchfloor-demo` repository and apply. `render.yaml` creates one **free** web service
   from the Dockerfile. (Or **New → Web Service**, pick the repository, runtime **Docker**,
   instance type **Free**.)
4. The first build takes about 5 minutes. The demo is then live at
   `https://watchfloor-demo.onrender.com` (Render shows the exact address).

To update it after a code change or a new snapshot, rebuild the bundle (the `.git` folder in
`build/demo` is kept), commit and push from `build/demo`; Render redeploys automatically.

A free Render service sleeps after 15 minutes without visitors; the next visit wakes it in
about a minute. Free services get 750 instance hours a month, enough to stay up all month.

Elsewhere: the same container runs on any Docker host, for example Google Cloud Run, or a
Hugging Face Docker Space (paid plan) if the README gets the Space card's YAML header with
`sdk: docker` and `app_port: 7860`.

## Data and terms

The snapshot contains public broadcasts: AIS via [aisstream.io](https://aisstream.io) and ADS-B
via the [OpenSky Network](https://opensky-network.org), whose data is for non-commercial use.
The demo is non-commercial, credits both sources on the map and in the bundle's README, and
publishes a few hours of positions for display, not a bulk dataset. Check both services'
current terms before publishing a new snapshot. The snapshot is git-ignored here and lives
only in the deploy repository.
