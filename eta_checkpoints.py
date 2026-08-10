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
    except Exception as e:
        logger.error(f"[ETA-CP] pass failed: {e}")
        return jsonify({"ok": False, "reason": str(e)[:200]}), 500

    return jsonify({"ok": True, "shadow": True, "examined": examined,
                    "windows": counts, "drivers_joined": joined,
                    "early_suppressed": suppressed}), 200
