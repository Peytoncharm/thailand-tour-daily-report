"""
booking_cache.py — Stage 1.2: read Zoho once, serve checkpoints locally
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Zoho stays the source of truth for WRITES. This module keeps a local
copy of booking records in Postgres (booking_cache table, created by
db.py) so high-frequency checkpoint logic reads the cache instead of
hammering the Zoho API.

Freshness model (deliberately NO Zoho "edit + repeat" rule — that would
fire on every automation field-write, e.g. GPS coords every 5 min, and
storm this webhook):
  1. Zoho Workflow Rule on CREATE  → POST /webhook/booking-cache-upsert
     → one Zoho read → upsert. New bookings cached within seconds.
  2. /cron/booking-cache-sweep every 15 min → 2 Zoho searches
     (today + tomorrow, full records) → bulk upsert. Hand edits in Zoho
     propagate within 15 min — same freshness the crons have today.
  3. Cache-miss fallback in get_booking(): one Zoho read + upsert, so a
     missed webhook self-heals on first use. Also works with the DB
     down (falls through to Zoho directly) — cache can degrade, never
     break the caller.

Loop safety: this module performs ZERO Zoho writes, so nothing here can
re-trigger a Zoho workflow rule.

Blueprint: booking_cache_bp
Endpoints:
  /webhook/booking-cache-upsert?key=<CRON_SECRET>   (POST, body: id)
  /cron/booking-cache-sweep                          (auth via app gate)
"""

import json
import logging
import os

import requests
from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)

booking_cache_bp = Blueprint("booking_cache", __name__)


# ─────────────────────────────────────────────────────────────
# Zoho fetch (full record — one API call includes every field)
# ─────────────────────────────────────────────────────────────

def _fetch_booking_from_zoho(booking_id: str):
    """One Zoho read of the full record. Returns dict or None."""
    try:
        from zoho_thailand import _get_access_token, ZOHO_API_BASE
        token = _get_access_token()
        if not token:
            return None
        resp = requests.get(
            f"{ZOHO_API_BASE}/Koh_Chang_Orders/{booking_id}",
            headers={"Authorization": f"Zoho-oauthtoken {token}"},
            timeout=10,
        )
        if resp.status_code != 200:
            logger.warning(f"[CACHE] Zoho fetch {booking_id} -> {resp.status_code}")
            return None
        data = resp.json().get("data") or []
        return data[0] if data else None
    except Exception as e:
        logger.error(f"[CACHE] Zoho fetch {booking_id} error: {e}")
        return None


# ─────────────────────────────────────────────────────────────
# Upsert
# ─────────────────────────────────────────────────────────────

def _extract_columns(rec: dict):
    """Pull the indexed columns out of a raw Zoho record."""
    prov = rec.get("Provider_List") or {}
    prov_id = prov.get("id") if isinstance(prov, dict) else None
    driver_code = None
    if prov_id:
        try:
            from gps_ingest import code_for_provider_id
            driver_code = code_for_provider_id(prov_id)
        except Exception:
            pass
    tour_date = (rec.get("Tour_Date") or "").split("T")[0] or None
    return {
        "booking_id": rec.get("id"),
        "provider_id": prov_id,
        "driver_id": driver_code,
        "tour_date": tour_date,
        "pickup_ts": rec.get("Pickup_Date_Time") or None,
        "status": rec.get("Status"),
        "type_of_package": rec.get("Type_of_Package"),
    }


def _maybe_geocode(rec: dict, cols: dict):
    """Stage 1.3 step 6 — KILL SWITCH: runs only when GEOCODE_ENABLED=true.
    If the booking has no Pickup_Lat yet, classify pickup+dropoff via
    pickup_matcher and (a) put coords on the cache row, (b) write
    Pickup_Lat/Lng/Zone + Route_Key back to Zoho ONCE per booking
    (trigger:[] so no workflow rules fire — loop-safe). Never raises."""
    if os.environ.get("GEOCODE_ENABLED", "").lower() != "true":
        return
    try:
        if rec.get("Pickup_Lat") is not None:
            return  # already geocoded
        from pickup_matcher import match_booking
        m = match_booking(rec.get("Pickup_Location"), rec.get("Dropoff_Location"))
        if not m.get("pickup_zone"):
            return
        cols["pickup_lat"] = m["pickup_lat"]
        cols["pickup_lng"] = m["pickup_lng"]
        cols["geocode_precision"] = m.get("geocode_precision")

        def _zoho_write():
            try:
                import requests as _rq
                from zoho_thailand import _get_access_token, ZOHO_API_BASE
                token = _get_access_token()
                if not token:
                    return
                fields = {"Pickup_Lat": m["pickup_lat"],
                          "Pickup_Lng": m["pickup_lng"],
                          "Pickup_Zone": m["pickup_zone"]}
                if m.get("route_key"):
                    fields["Route_Key"] = m["route_key"]
                _rq.put(f"{ZOHO_API_BASE}/Koh_Chang_Orders/{rec['id']}",
                        headers={"Authorization": f"Zoho-oauthtoken {token}",
                                 "Content-Type": "application/json"},
                        json={"data": [fields], "trigger": []}, timeout=10)
                logger.info(f"[GEOCODE] {rec['id']}: {m['pickup_zone']} "
                            f"({m['pickup_precision']}) route={m.get('route_key')}")
            except Exception as e:
                logger.warning(f"[GEOCODE] Zoho write failed {rec.get('id')}: {e}")
        import threading as _th
        _th.Thread(target=_zoho_write, daemon=True).start()
    except Exception as e:
        logger.warning(f"[GEOCODE] match failed {rec.get('id')}: {e}")


def upsert_record(rec: dict) -> bool:
    """UPSERT one raw Zoho record into booking_cache. Never raises."""
    if not isinstance(rec, dict) or not rec.get("id"):
        return False
    try:
        from db import _get_pool, ensure_schema
        pool = _get_pool()
        if pool is None or not ensure_schema():
            return False
        cols = _extract_columns(rec)
        _maybe_geocode(rec, cols)
        with pool.connection() as conn:
            conn.execute(
                """
                INSERT INTO booking_cache
                  (booking_id, provider_id, driver_id, tour_date, pickup_ts,
                   status, type_of_package, pickup_lat, pickup_lng,
                   geocode_precision, payload, refreshed_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, now())
                ON CONFLICT (booking_id) DO UPDATE SET
                  provider_id = EXCLUDED.provider_id,
                  driver_id = EXCLUDED.driver_id,
                  tour_date = EXCLUDED.tour_date,
                  pickup_ts = EXCLUDED.pickup_ts,
                  status = EXCLUDED.status,
                  type_of_package = EXCLUDED.type_of_package,
                  pickup_lat = COALESCE(EXCLUDED.pickup_lat, booking_cache.pickup_lat),
                  pickup_lng = COALESCE(EXCLUDED.pickup_lng, booking_cache.pickup_lng),
                  geocode_precision = COALESCE(EXCLUDED.geocode_precision,
                                               booking_cache.geocode_precision),
                  payload = EXCLUDED.payload,
                  refreshed_at = now()
                """,
                (cols["booking_id"], cols["provider_id"], cols["driver_id"],
                 cols["tour_date"], cols["pickup_ts"], cols["status"],
                 cols["type_of_package"], cols.get("pickup_lat"),
                 cols.get("pickup_lng"), cols.get("geocode_precision"),
                 json.dumps(rec)),
            )
        return True
    except Exception as e:
        logger.warning(f"[CACHE] upsert {rec.get('id')} failed: {e}")
        return False


# ─────────────────────────────────────────────────────────────
# Read helpers (for checkpoint conversion in the next step)
# ─────────────────────────────────────────────────────────────

def get_booking(booking_id: str):
    """Cache-first read of one booking (raw Zoho-shaped dict).
    Miss or DB failure → one Zoho read (+ upsert on success).
    Returns None only if BOTH cache and Zoho fail."""
    if not booking_id:
        return None
    try:
        from db import _get_pool
        pool = _get_pool()
        if pool is not None:
            with pool.connection() as conn:
                row = conn.execute(
                    "SELECT payload FROM booking_cache WHERE booking_id = %s",
                    (booking_id,),
                ).fetchone()
            if row and row[0]:
                return row[0] if isinstance(row[0], dict) else json.loads(row[0])
    except Exception as e:
        logger.warning(f"[CACHE] read {booking_id} failed (falling back to Zoho): {e}")
    rec = _fetch_booking_from_zoho(booking_id)
    if rec:
        upsert_record(rec)
    return rec


def update_cached_field(booking_id: str, field: str, value) -> bool:
    """Patch ONE field inside the cached payload right after a Zoho write.
    Race fix for the cron family: approach-send flags a booking at :10,
    auto-rebroadcast reads the cache at :13 — without this patch it would
    not see the flag until the :18 sweep and could re-fire the job.
    DB-local write only (zero Zoho traffic). Never raises."""
    if not booking_id or not field:
        return False
    try:
        from db import _get_pool
        pool = _get_pool()
        if pool is None:
            return False
        with pool.connection() as conn:
            conn.execute(
                "UPDATE booking_cache SET payload = jsonb_set(payload, %s, %s::jsonb) "
                "WHERE booking_id = %s",
                ([field], json.dumps(value), booking_id),
            )
        return True
    except Exception as e:
        logger.warning(f"[CACHE] field patch {booking_id}.{field} failed: {e}")
        return False


def get_bookings_for_dates(dates, type_of_package=None):
    """All cached bookings whose Tour_Date is in `dates` (list of
    'YYYY-MM-DD'), optionally filtered by Type_of_Package. Returns raw
    Zoho-shaped dicts so existing cron logic can consume them unchanged.
    Returns None (not []) on DB failure so callers can fall back."""
    try:
        from db import _get_pool
        pool = _get_pool()
        if pool is None:
            return None
        sql = "SELECT payload FROM booking_cache WHERE tour_date = ANY(%s)"
        params = [list(dates)]
        if type_of_package:
            sql += " AND type_of_package = %s"
            params.append(type_of_package)
        with pool.connection() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [r[0] if isinstance(r[0], dict) else json.loads(r[0]) for r in rows]
    except Exception as e:
        logger.warning(f"[CACHE] window read failed: {e}")
        return None


# ─────────────────────────────────────────────────────────────
# Endpoints
# ─────────────────────────────────────────────────────────────

@booking_cache_bp.route("/webhook/booking-cache-upsert", methods=["POST"])
def webhook_booking_cache_upsert():
    """Zoho Workflow Rule (on CREATE) posts {id} here. Same auth pattern
    as /gps/status: ?key=CRON_SECRET in the URL (Zoho webhooks cannot
    send headers). Performs ONE Zoho read, zero Zoho writes."""
    cron_secret = os.environ.get("CRON_SECRET", "")
    if cron_secret and request.args.get("key", "") != cron_secret:
        return jsonify({"error": "unauthorized"}), 401

    params = request.form if request.form else (request.get_json(silent=True) or {})
    booking_id = str(params.get("id") or params.get("record_id") or "").strip()
    if not booking_id:
        return jsonify({"ok": False, "reason": "no id in payload"}), 200

    rec = _fetch_booking_from_zoho(booking_id)
    if not rec:
        return jsonify({"ok": False, "reason": "zoho fetch failed"}), 200
    ok = upsert_record(rec)
    logger.info(f"[CACHE] webhook upsert {booking_id}: cached={ok}")
    return jsonify({"ok": ok, "booking_id": booking_id}), 200


@booking_cache_bp.route("/cron/booking-cache-sweep", methods=["GET", "POST"])
def cron_booking_cache_sweep():
    """Every 15 min via cron-job.org: refresh today+tomorrow bookings
    (2 Zoho searches, full records). This is the freshness backbone —
    hand edits in Zoho propagate within one sweep, same latency the
    direct-read crons have today. Auth: app-level /cron gate."""
    from datetime import datetime, timezone, timedelta
    ict = timezone(timedelta(hours=7))
    now = datetime.now(ict)
    days = [now.strftime("%Y-%m-%d"),
            (now + timedelta(days=1)).strftime("%Y-%m-%d")]
    fetched, cached = 0, 0
    try:
        from zoho_thailand import zoho_search
        for day in days:
            records = zoho_search("Koh_Chang_Orders", f"(Tour_Date:equals:{day})")
            fetched += len(records)
            for rec in records:
                if upsert_record(rec):
                    cached += 1
    except Exception as e:
        logger.error(f"[CACHE] sweep failed: {e}")
        return jsonify({"ok": False, "reason": str(e)[:200],
                        "fetched": fetched, "cached": cached}), 500
    logger.info(f"[CACHE] sweep done: fetched={fetched} cached={cached}")
    return jsonify({"ok": True, "days": days,
                    "fetched": fetched, "cached": cached}), 200
