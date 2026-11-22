# Watchfloor public demo: the operator console on a read-only data snapshot.
# Built from the folder that scripts/build_demo_bundle.py assembles (see docs/DEMO.md).
# No credentials: there is no Snowflake, Kafka or Claude API access in this image.
FROM python:3.12-slim

# Run as an unprivileged user (uid 1000 also suits Hugging Face Spaces).
RUN useradd --create-home --uid 1000 user
WORKDIR /app

COPY --chown=user pyproject.toml README.md ./
COPY --chown=user src ./src
RUN pip install --no-cache-dir . gunicorn==23.0.0

COPY --chown=user demo_snapshot ./demo_snapshot

USER user
ENV DEMO_DIR=/app/demo_snapshot \
    AUDIT_LOG_PATH=/tmp/audit.jsonl \
    PYTHONUNBUFFERED=1
EXPOSE 7860

# Hosts such as Render assign the port in $PORT; 7860 otherwise.
CMD exec gunicorn --bind "0.0.0.0:${PORT:-7860}" --workers 1 --threads 4 --timeout 120 \
    "watchfloor.dashboard.app:create_server()"
