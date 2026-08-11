"""
eta_checkpoints.py — Stage 2 Step 1: checkpoint skeleton (SHADOW ONLY)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Every 15 min (cron-job.org at :06/:21/:36/:51): classify today's and
tomorrow's cached bookings into T-120/90/60/30 pre-pickup windows, join
the assigned driver's latest position, and LOG — no ETA math, no
messages, nothing sent. This is the scaffold later steps hang off.

Early-pickup suppression (Orathai, 10 Aug): island bookings with pickup
before 09:00 are marked suppressed-until-T90 in shadow from day one.

Reads ONLY booking_cache + driver_latest. Zero Zoho. Zero writes.
Auth: app-level /cron gate.
"""

import logging
from datetime import datetime, timezone, timedelta

from flask import Blueprint, jsonify

logger = logging.getLogger(__name__)

eta_bp = Blueprint("eta_checkpoints", __name__)

ICT = timezone(timedelta(hours=7))

ISLAND_ZONES = {"white sand beach", "klong prao", "kai bae", "lonely beach",
                "bang bao pier", "salak phet", "klong son", "bailan beach",
                "koh chang generic"}
EARLY_CUTOFF_MIN = 9 * 60      # island cutoff 09:00 (D-P2, locked)

WINDOWS = [("T-30", 0, 30), ("T-60", 30, 60), ("T-90", 60, 90), ("T-120", 90, 120)]


def _window_for(minutes_to_pickup):
    for name, lo, hi in WINDOWS:
        if lo < minutes_to_pickup <= hi:
            return name
    return None


@eta_bp.route("/cron/eta-checkpoints", methods=["GET", "POST"])
def eta_checkpoints():
    now = datetime.now(ICT)
    days = [now.strftime("%Y-%m-%d"), (now + timedelta(days=1)).strftime("%Y-%m-%d")]
    counts = {"T-30": 0, "T-60": 0, "T-90": 0, "T-120": 0}
    suppressed = 0
    joined = 0
    examined = 0
    try:
        from booking_cache import get_bookings_for_dates
        from db import _get_pool
        bookings = get_bookings_for_dates(days) or []

        latest = {}
        try:
            pool = _get_pool()
            if pool is not None:
                with pool.connection() as conn:
                    for code, ts in conn.execute(
                            "SELECT driver_id, ts FROM driver_latest").fetchall():
                        latest[code] = ts
        except Exception as e:
            logger.warning(f"[ETA-CP] driver_latest join failed: {e}")

        from gps_ingest import code_for_provider_id
        for b in bookings:
            pdt = b.get("Pickup_Date_Time") or ""
            if "T" not in pdt:
                continue
            try:
                pk = datetime.fromisoformat(pdt)
            except ValueError:
                continue
            m = (pk - now).total_seconds() / 60
            win = _window_for(m)
            if win is None:
                continue
            examined += 1
            counts[win] += 1

            zone = (b.get("Pickup_Zone") or "").strip().lower()
            if not zone:
                # Payload zone lags one sweep behind the geocoder (found at
                # the Step-1 gate, 11 Aug): classify the raw string live so
                # suppression is timing-independent.
                try:
                    from pickup_matcher import classify
                    zone = (classify(b.get("Pickup_Location") or "")[0] or "").lower()
                except Exception:
                    zone = ""
            pk_min = pk.hour * 60 + pk.minute
            early = zone in ISLAND_ZONES and pk_min < EARLY_CUTOFF_MIN
            sup = early and win in ("T-120",)  # suppressed before T-90
            if sup:
                suppressed += 1

            drv_code = None
            prov = b.get("Provider_List")
            if isinstance(prov, dict) and prov.get("id"):
                try:
                    drv_code = code_for_provider_id(prov["id"])
                except Exception:
                    drv_code = None
            age_s = None
            if drv_code and drv_code in latest:
                joined += 1
                try:
                    age_s = int((datetime.now(timezone.utc) - latest[drv_code]).total_seconds())
                except Exception:
                    pass

            logger.info(
                f"[ETA-CP] booking={b.get('id')} window={win} "
                f"pickup={pdt[:16]} zone={zone or '-'} "
                f"driver={drv_code or 'unassigned'} age={age_s if age_s is not None else '-'}s "
                f"{'SUPPRESSED-until-T90 ' if sup else ''}(shadow)"
            )
            # Step 2: open a skeleton eta_history row for this booking's
            # window (once per booking+window+day) — the ledger later
            # steps attach predictions to, and the completion writer
            # closes with actual_sec.
            _open_skeleton_row(b, win, drv_code)

        opened, closed = _completion_pass()
    except Exception as e:
        logger.error(f"[ETA-CP] pass failed: {e}")
        return jsonify({"ok": False, "reason": str(e)[:200]}), 500

    return jsonify({"ok": True, "shadow": True, "examined": examined,
                    "windows": counts, "drivers_joined": joined,
                    "early_suppressed": suppressed,
                    "eta_rows_opened": opened, "eta_rows_closed": closed}), 200


# ─────────────────────────────────────────────────────────────
# Step 2 — eta_history skeleton rows + actual_sec completion
# ─────────────────────────────────────────────────────────────

_opened_this_pass = 0

def _open_skeleton_row(b, win, drv_code):
    """One open eta_history row per booking+window+day. Never raises."""
    global _opened_this_pass
    try:
        from db import _get_pool
        pool = _get_pool()
        if pool is None:
            return
        method = f"checkpoint:{win}"
        with pool.connection() as conn:
            exists = conn.execute(
                "SELECT 1 FROM eta_history WHERE booking_id = %s AND method = %s "
                "AND computed_at::date = (now() AT TIME ZONE 'Asia/Bangkok')::date LIMIT 1",
                (str(b.get("id")), method),
            ).fetchone()
            if exists:
                return
            conn.execute(
                "INSERT INTO eta_history (booking_id, driver_id, route_key, method) "
                "VALUES (%s, %s, %s, %s)",
                (str(b.get("id")), drv_code,
                 (b.get("Route_Key") or None), method),
            )
        _opened_this_pass += 1
    except Exception as e:
        logger.warning(f"[ETA-CP] skeleton row failed for {b.get('id')}: {e}")


def _haversine_m(lat1, lng1, lat2, lng2):
    from math import radians, sin, cos, asin, sqrt
    R = 6371000.0
    dlat, dlng = radians(lat2 - lat1), radians(lng2 - lng1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlng / 2) ** 2
    return 2 * R * asin(sqrt(a))

ARRIVAL_RADIUS_M = 300

def _completion_pass():
    """Close open checkpoint rows: driver's first GPS fix within 300 m of
    the booking's pickup pin (from driver_positions) => actual_sec =
    arrival - computed_at, on_time = arrived before pickup_ts. Rows
    without observable arrival stay open (harmless). Never raises."""
    global _opened_this_pass
    opened, _opened_this_pass = _opened_this_pass, 0
    closed = 0
    try:
        from db import _get_pool
        pool = _get_pool()
        if pool is None:
            return opened, 0
        with pool.connection() as conn:
            open_rows = conn.execute(
                "SELECT h.id, h.booking_id, h.driver_id, h.computed_at, "
                "       c.pickup_lat, c.pickup_lng, c.pickup_ts "
                "FROM eta_history h JOIN booking_cache c ON c.booking_id = h.booking_id "
                "WHERE h.actual_sec IS NULL AND h.method LIKE 'checkpoint:%%' "
                "  AND h.computed_at > now() - interval '24 hours' "
                "  AND c.pickup_lat IS NOT NULL AND h.driver_id IS NOT NULL "
                "LIMIT 200"
            ).fetchall()
            for hid, bid, drv, computed_at, plat, plng, pickup_ts in open_rows:
                pts = conn.execute(
                    "SELECT ts, lat, lng FROM driver_positions "
                    "WHERE driver_id = %s AND ts >= %s ORDER BY ts LIMIT 500",
                    (drv, computed_at),
                ).fetchall()
                arrival = None
                for ts, lat, lng in pts:
                    if _haversine_m(lat, lng, plat, plng) <= ARRIVAL_RADIUS_M:
                        arrival = ts
                        break
                if arrival is None:
                    continue
                actual = int((arrival - computed_at).total_seconds())
                on_time = bool(pickup_ts and arrival <= pickup_ts)
                conn.execute(
                    "UPDATE eta_history SET actual_sec = %s, on_time = %s WHERE id = %s",
                    (actual, on_time, hid),
                )
                closed += 1
                logger.info(f"[ETA-CP] closed row {hid} booking={bid} "
                            f"actual={actual}s on_time={on_time}")
    except Exception as e:
        logger.warning(f"[ETA-CP] completion pass failed: {e}")
    return opened, closed
