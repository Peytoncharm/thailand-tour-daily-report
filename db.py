"""
db.py — Postgres foundation (Stage 1.1 of the scaling plan)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Durable store for GPS positions + the tables the precision layer will
use (booking_cache, eta_history with predicted-vs-actual learning,
alert_log). V1 is DUAL-WRITE: gps_ingest keeps its in-memory store as
the source of truth for reads; Postgres rides along so nothing breaks
if the database hiccups. Reads cut over in a later, separate change.

Blueprint: db_bp
Endpoints:
  /cron/db-purge   — nightly retention purge (auth via the app-level
                     /cron/ CRON_SECRET gate in app.py)

Env vars:
  DATABASE_URL — Render internal connection string (set in Render UI)
  GPS_DB_WRITE — "true" enables dual-writes; anything else = kill switch

Fail-safe contract: NOTHING in this module ever raises to a caller.
A dead database costs log lines, never a failed ping or a 500.
"""

import logging
import os
import threading

from flask import Blueprint, jsonify

logger = logging.getLogger(__name__)

db_bp = Blueprint("db", __name__)

DATABASE_URL = os.environ.get("DATABASE_URL", "")

_pool = None
_pool_lock = threading.Lock()
_schema_ready = False


def enabled() -> bool:
    """Dual-writes on only when BOTH the URL and the flag are set."""
    return bool(DATABASE_URL) and os.environ.get("GPS_DB_WRITE", "").lower() == "true"


def _get_pool():
    """Lazy pool init. Small pool — Basic-256MB has few connections and
    our write rate is ~1-2/s. Never raises; returns None on failure."""
    global _pool
    if _pool is not None:
        return _pool
    with _pool_lock:
        if _pool is not None:
            return _pool
        if not DATABASE_URL:
            return None
        try:
            from psycopg_pool import ConnectionPool
            _pool = ConnectionPool(
                DATABASE_URL,
                min_size=0,
                max_size=4,
                timeout=3,               # max wait for a pooled connection
                kwargs={
                    "connect_timeout": 3,
                    "options": "-c statement_timeout=4000",
                },
            )
            logger.info("[GPS-DB] connection pool created")
        except Exception as e:
            logger.error(f"[GPS-DB] pool init failed: {e}")
            _pool = None
    return _pool


_SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS driver_positions (
  id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  driver_id   text NOT NULL,
  ts          timestamptz NOT NULL,
  lat         double precision NOT NULL,
  lng         double precision NOT NULL,
  speed real, bearing real, altitude real, accuracy real, batt real,
  received_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_driver_positions_driver_ts
  ON driver_positions (driver_id, ts DESC);

CREATE TABLE IF NOT EXISTS driver_latest (
  driver_id  text PRIMARY KEY,
  ts timestamptz NOT NULL,
  lat double precision NOT NULL,
  lng double precision NOT NULL,
  speed real, bearing real, accuracy real, batt real,
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS booking_cache (
  booking_id text PRIMARY KEY,
  provider_id text,
  driver_id text,
  tour_date date,
  pickup_ts timestamptz,
  pickup_lat double precision,
  pickup_lng double precision,
  status text,
  type_of_package text,
  payload jsonb,
  refreshed_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS eta_history (
  id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  booking_id    text NOT NULL,
  driver_id     text,
  route_key     text,
  computed_at   timestamptz NOT NULL DEFAULT now(),
  distance_km   real,
  predicted_sec integer,
  actual_sec    integer,
  method        text,
  on_time       boolean
);
CREATE INDEX IF NOT EXISTS idx_eta_history_booking
  ON eta_history (booking_id, computed_at DESC);
CREATE INDEX IF NOT EXISTS idx_eta_history_route
  ON eta_history (route_key, computed_at DESC);

CREATE TABLE IF NOT EXISTS alert_log (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  booking_id text,
  driver_id  text,
  alert_type text NOT NULL,
  sent_at    timestamptz NOT NULL DEFAULT now(),
  channel    text,
  detail     text
);
CREATE INDEX IF NOT EXISTS idx_alert_log_booking
  ON alert_log (booking_id, sent_at DESC);
"""


def ensure_schema():
    """Idempotent DDL — safe to run on every boot. Never raises."""
    global _schema_ready
    if _schema_ready:
        return True
    pool = _get_pool()
    if pool is None:
        return False
    try:
        with pool.connection() as conn:
            conn.execute(_SCHEMA_DDL)
        _schema_ready = True
        logger.info("[GPS-DB] schema ensured (5 tables)")
        return True
    except Exception as e:
        logger.error(f"[GPS-DB] ensure_schema failed: {e}")
        return False


def ensure_schema_async():
    """Boot-time schema check without delaying app startup."""
    if DATABASE_URL:
        threading.Thread(target=ensure_schema, daemon=True).start()


def insert_position(driver_id: str, point: dict, batt=None):
    """Dual-write one accepted ping: append to driver_positions and
    UPSERT driver_latest, in one transaction. Called from a daemon
    thread by gps_ingest — never raises, never blocks the ping."""
    pool = _get_pool()
    if pool is None:
        return
    if not _schema_ready and not ensure_schema():
        return
    try:
        with pool.connection() as conn:
            conn.execute(
                """
                INSERT INTO driver_positions
                  (driver_id, ts, lat, lng, speed, bearing, altitude, accuracy, batt)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (driver_id, point["ts"], point["lat"], point["lng"],
                 point.get("speed"), point.get("bearing"),
                 point.get("altitude"), point.get("accuracy"), batt),
            )
            conn.execute(
                """
                INSERT INTO driver_latest
                  (driver_id, ts, lat, lng, speed, bearing, accuracy, batt, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now())
                ON CONFLICT (driver_id) DO UPDATE SET
                  ts = EXCLUDED.ts, lat = EXCLUDED.lat, lng = EXCLUDED.lng,
                  speed = EXCLUDED.speed, bearing = EXCLUDED.bearing,
                  accuracy = EXCLUDED.accuracy, batt = EXCLUDED.batt,
                  updated_at = now()
                """,
                (driver_id, point["ts"], point["lat"], point["lng"],
                 point.get("speed"), point.get("bearing"),
                 point.get("accuracy"), batt),
            )
    except Exception as e:
        logger.warning(f"[GPS-DB] insert_position failed for {driver_id}: {e}")


def db_status(driver_id: str) -> dict:
    """Read-only stats for /gps/status debug route. Never raises."""
    pool = _get_pool()
    if pool is None:
        return {"enabled": enabled(), "connected": False}
    try:
        with pool.connection() as conn:
            row = conn.execute(
                "SELECT count(*), max(ts) FROM driver_positions WHERE driver_id = %s",
                (driver_id,),
            ).fetchone()
        return {
            "enabled": enabled(),
            "connected": True,
            "rows": row[0],
            "latest_ts": row[1].isoformat() if row[1] else None,
        }
    except Exception as e:
        return {"enabled": enabled(), "connected": False, "error": str(e)[:120]}


# ─────────────────────────────────────────────────────────────
# Nightly purge — auth handled by app.py's /cron/ gate
# ─────────────────────────────────────────────────────────────

_PURGES = [
    ("driver_positions", "DELETE FROM driver_positions WHERE ts < now() - interval '30 days'"),
    ("eta_history",      "DELETE FROM eta_history WHERE computed_at < now() - interval '30 days'"),
    ("alert_log",        "DELETE FROM alert_log WHERE sent_at < now() - interval '90 days'"),
    ("booking_cache",    "DELETE FROM booking_cache WHERE tour_date < (now() - interval '60 days')::date"),
]


@db_bp.route("/cron/db-purge", methods=["GET"])
def cron_db_purge():
    """Retention purge — schedule on cron-job.org ~03:30 ICT daily."""
    pool = _get_pool()
    if pool is None:
        return jsonify({"ok": False, "reason": "no DATABASE_URL / pool"}), 200
    if not ensure_schema():
        return jsonify({"ok": False, "reason": "schema not ready"}), 200
    deleted = {}
    try:
        with pool.connection() as conn:
            for table, sql in _PURGES:
                cur = conn.execute(sql)
                deleted[table] = cur.rowcount
        logger.info(f"[GPS-DB] purge done: {deleted}")
        return jsonify({"ok": True, "deleted": deleted}), 200
    except Exception as e:
        logger.error(f"[GPS-DB] purge failed: {e}")
        return jsonify({"ok": False, "reason": str(e)[:200]}), 200
