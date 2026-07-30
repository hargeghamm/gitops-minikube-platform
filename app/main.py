"""QOVES take-home API - deliberately trivial.

GET / -> hello (liveness; no dependencies)
GET /healthz -> runs `SELECT 1` against Postgres; 200 if reachable, 503 if not
GET /metrics -> Prometheus exposition format

The DB connection string is read from DATABASE_URL and is NOT hard-coded - it
must arrive from a Secret at runtime.
"""

import os

import psycopg
from flask import Flask, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, generate_latest

app = Flask(__name__)
DATABASE_URL = os.environ.get("DATABASE_URL", "")
REQUESTS = Counter("http_requests_total", "Total HTTP requests", ["path", "status"])


@app.get("/")
def hello():
    REQUESTS.labels("/", "200").inc()
    return "hello from the QOVES take-home API\n"


@app.get("/healthz")
def healthz():
    try:
        with psycopg.connect(DATABASE_URL, connect_timeout=3) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
        REQUESTS.labels("/healthz", "200").inc()
        return "ok\n", 200
    except Exception as exc:
        REQUESTS.labels("/healthz", "503").inc()
        return f"db unreachable: {exc}\n", 503


@app.get("/metrics")
def metrics():
    return Response(generate_latest(), mimetype=CONTENT_TYPE_LATEST)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)  # local dev only; container runs gunicorn
