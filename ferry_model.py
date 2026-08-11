"""
ferry_model.py — Stage 2 Step 5: ferry model data + queue-learning (SHADOW)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Ao Thammachat ↔ Ao Sapparot is the ONLY car ferry. Four route segments:
drive_to_pier + queue + crossing + island_leg. Timetable and queue
baselines live in ferry_model.json (EDITABLE DATA — reloaded per pass).

Queue-learning loop (spec §3 Step 5): from GPS history, detect
pier-arrival (within PIER_RADIUS_M of a pier pin) followed by mid-water
(inside the strait corridor, well clear of both piers). Per spec:
  measured_queue_sec = t(mid-water) − t(pier-arrival) − crossing
Raw timestamps are logged alongside so the formula's slack (mid-water
fires DURING the crossing, not after it) is visible at gate review.
Negative results are stored as-is and flagged — data quality is part of
what the shadow phase measures.

Measurements are written to eta_history with booking_id='FERRY-QUEUE',
method='ferry-queue-observed', route_key='ferry-queue:<weekday>:<hour>'
(actual_sec = measured queue, predicted_sec = JSON baseline for drift
comparison). SHADOW: nothing reads these rows yet; predictions are
unaffected until Orathai approves observed baselines.

/cron/ferry-queue-replay — scan ALL driver_positions history (idempotent:
one row per driver+arrival). Auth: app-level /cron gate.
"""

import json
import logging
import os
from datetime import datetime, time, timedelta, timezone

from flask import Blueprint, jsonify

logger = logging.getLogger(__name__)

ferry_bp = Blueprint("ferry_model", __name__)

ICT = timezone(timedelta(hours=7))

# Zone sets for positioning (P-B). Island set mirrors
# eta_checkpoints.ISLAND_ZONES (kept separately there to avoid an
# import cycle) plus the island-side pier itself.
ISLAND_ZONES = {"white sand beach", "klong prao", "kai bae", "lonely beach",
                "bang bao pier", "salak phet", "klong son", "bailan beach",
                "koh chang generic", "ao sapparot pier"}
MAINLAND_NEAR_FERRY = {"ao thammachat pier", "laem ngop pier", "trat town",
                       "trat airport", "laem sok pier"}

_MODEL_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "ferry_model.json")
_model_cache = {"mtime": None, "data": None}

PIER_RADIUS_M = 300      # same arrival radius as the completion pass
MIDWATER_CLEAR_M = 600   # mid-water = inside corridor AND this far from both piers

# Strait corridor between the two piers (crude bbox, shadow-tier)
_CORRIDOR = (12.145, 12.185, 102.265, 102.310)


def load_model():
    """ferry_model.json with mtime-based reload — edits take effect on
    the next pass, no code change."""
    try:
        mt = os.path.getmtime(_MODEL_PATH)
        if _model_cache["mtime"] != mt:
            with open(_MODEL_PATH, encoding="utf-8") as f:
                _model_cache["data"] = json.load(f)
            _model_cache["mtime"] = mt
    except Exception as e:
        logger.error(f"[FERRY] model load failed: {e}")
    return _model_cache["data"] or {}


def _pier_pins():
    from pickup_matcher import _load_points
    pts = _load_points()
    m = load_model()
    main_p = pts.get(m.get("pier_mainland", "ao thammachat pier"))
    isl_p = pts.get(m.get("pier_island", "ao sapparot pier"))
    return ((main_p["lat"], main_p["lng"]) if main_p else None,
            (isl_p["lat"], isl_p["lng"]) if isl_p else None)


def _haversine_m(lat1, lng1, lat2, lng2):
    from math import radians, sin, cos, asin, sqrt
    R = 6371000.0
    dlat, dlng = radians(lat2 - lat1), radians(lng2 - lng1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlng / 2) ** 2
    return 2 * R * asin(sqrt(a))


def _model_minutes(model, key, default_hhmm):
    try:
        hh, mm = str(model.get(key, default_hhmm)).split(":")
        return int(hh) * 60 + int(mm)
    except Exception:
        hh, mm = default_hhmm.split(":")
        return int(hh) * 60 + int(mm)


def positioning_for(zone, pickup_dt):
    """P-B (Layer 1): (positioning_required, position_deadline) for a
    booking, from the editable model. zone is lowercase; pickup_dt is a
    tz-aware ICT datetime. (None, None) when no positioning is needed
    or inputs are unusable. All cutoffs/deadlines are DATA (the JSON),
    not code — they shift with the ferry timetable and season."""
    z = (zone or "").strip().lower()
    if not z or pickup_dt is None:
        return None, None
    m = load_model()
    pk_min = pickup_dt.hour * 60 + pickup_dt.minute
    day_before = (pickup_dt - timedelta(days=1)).date()
    if z in ISLAND_ZONES and pk_min < _model_minutes(m, "island_cutoff", "09:00"):
        # D-P1: the 17:45 boat the evening before is the deadline
        dl = _model_minutes(m, "positioning_deadline", "17:45")
        return ("island overnight",
                datetime.combine(day_before, time(dl // 60, dl % 60), tzinfo=ICT))
    if z in MAINLAND_NEAR_FERRY and pk_min < _model_minutes(m, "mainland_cutoff", "07:00"):
        # deadline = last island->mainland sailing the evening before
        try:
            hh, mm = str(m.get("sailings", {}).get("last", "18:30")).split(":")
            dl = int(hh) * 60 + int(mm)
        except Exception:
            dl = 18 * 60 + 30
        return ("mainland overnight",
                datetime.combine(day_before, time(dl // 60, dl % 60), tzinfo=ICT))
    return None, None


def near_pier(lat, lng, piers):
    main_pin, isl_pin = piers
    if main_pin and _haversine_m(lat, lng, *main_pin) <= PIER_RADIUS_M:
        return "mainland"
    if isl_pin and _haversine_m(lat, lng, *isl_pin) <= PIER_RADIUS_M:
        return "island"
    return None


def is_midwater(lat, lng, piers):
    if not (_CORRIDOR[0] <= lat <= _CORRIDOR[1]
            and _CORRIDOR[2] <= lng <= _CORRIDOR[3]):
        return False
    main_pin, isl_pin = piers
    for pin in (main_pin, isl_pin):
        if pin and _haversine_m(lat, lng, *pin) < MIDWATER_CLEAR_M:
            return False
    return True


@ferry_bp.route("/cron/ferry-queue-replay", methods=["GET", "POST"])
def ferry_queue_replay():
    """Replay ALL GPS history for pier-arrival → mid-water sequences and
    write one ferry-queue-observed row per crossing found. Idempotent."""
    model = load_model()
    crossing_sec = int(model.get("crossing_min", 30)) * 60
    baseline_sec = int(model.get("queue_min_baseline", {}).get("default", 15)) * 60
    piers = _pier_pins()
    if piers[0] is None or piers[1] is None:
        return jsonify({"ok": False, "reason": "pier pins missing from zone table"}), 500

    found, written, skipped_dup, negative = 0, 0, 0, 0
    crossings = []
    try:
        from db import _direct_conn
        with _direct_conn("ferry-replay") as conn:
            drivers = [r[0] for r in conn.execute(
                "SELECT DISTINCT driver_id FROM driver_positions").fetchall()]
            for drv in drivers:
                rows = conn.execute(
                    "SELECT ts, lat, lng FROM driver_positions "
                    "WHERE driver_id = %s ORDER BY ts", (drv,)).fetchall()
                arrival_ts, arrival_side = None, None
                for ts, lat, lng in rows:
                    side = near_pier(lat, lng, piers)
                    if side:
                        # keep the FIRST fix of a pier dwell; reset if a
                        # later fix shows a different pier (new approach)
                        if arrival_side != side:
                            arrival_ts, arrival_side = ts, side
                        continue
                    if arrival_ts and is_midwater(lat, lng, piers):
                        # a crossing: pier-arrival followed by mid-water
                        if ts - arrival_ts > timedelta(hours=6):
                            arrival_ts, arrival_side = None, None
                            continue  # stale dwell — not one event
                        found += 1
                        queue_sec = int((ts - arrival_ts).total_seconds()) - crossing_sec
                        if queue_sec < 0:
                            negative += 1
                        arr_ict = arrival_ts + timedelta(hours=7)
                        rk = f"ferry-queue:{arr_ict.strftime('%a').lower()}:{arr_ict.hour:02d}"
                        dup = conn.execute(
                            "SELECT 1 FROM eta_history WHERE booking_id = 'FERRY-QUEUE' "
                            "AND driver_id = %s AND computed_at = %s LIMIT 1",
                            (drv, arrival_ts)).fetchone()
                        if dup:
                            skipped_dup += 1
                        else:
                            conn.execute(
                                "INSERT INTO eta_history (booking_id, driver_id, "
                                "route_key, computed_at, predicted_sec, actual_sec, method) "
                                "VALUES ('FERRY-QUEUE', %s, %s, %s, %s, %s, "
                                "'ferry-queue-observed')",
                                (drv, rk, arrival_ts, baseline_sec, queue_sec))
                            written += 1
                        if len(crossings) < 10:
                            crossings.append({
                                "driver": drv, "side": arrival_side,
                                "pier_arrival": arrival_ts.isoformat(),
                                "midwater": ts.isoformat(),
                                "queue_sec": queue_sec, "route_key": rk,
                                "negative_flag": queue_sec < 0,
                            })
                        logger.info(f"[FERRY] crossing driver={drv} side={arrival_side} "
                                    f"arrival={arrival_ts} midwater={ts} "
                                    f"queue={queue_sec}s{' NEGATIVE' if queue_sec < 0 else ''} (shadow)")
                        arrival_ts, arrival_side = None, None
    except Exception as e:
        logger.error(f"[FERRY] replay failed: {e}")
        return jsonify({"ok": False, "reason": str(e)[:200]}), 500

    return jsonify({"ok": True, "shadow": True,
                    "model": {"crossing_min": model.get("crossing_min"),
                              "baseline_queue_min": model.get("queue_min_baseline", {}).get("default")},
                    "drivers_scanned": len(drivers),
                    "crossings_found": found, "rows_written": written,
                    "duplicates_skipped": skipped_dup,
                    "negative_queue_flags": negative,
                    "crossings": crossings}), 200
