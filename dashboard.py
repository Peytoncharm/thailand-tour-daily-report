"""
dashboard.py — Team dashboard map (Stage: dashboard slice 1, 9 Aug 2026)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Slice 1: full-viewport live map of all tracked drivers.
  /team/dashboard        — page (Leaflet + OSM, dark board aesthetic)
  /team/dashboard/data   — JSON: one entry per driver_latest row

Auth: ?key=CRON_SECRET on BOTH routes (gps_status pattern — the /team/
prefix is not covered by the app-level gate, and an all-drivers map must
never be public).

READS ONLY: driver_latest (Postgres) + the in-memory provider registry
for display names. No writes anywhere, no contact with the ingest path.

Slices 2+ (not built): booking/job overlay from booking_cache, status
filters, alert strip, attention budget.
"""

import logging
import os
from datetime import datetime, timezone, timedelta

from flask import Blueprint, jsonify, render_template, request

logger = logging.getLogger(__name__)

dashboard_bp = Blueprint("dashboard", __name__)

ICT = timezone(timedelta(hours=7))

# Signal-age thresholds (seconds) — constants for slice 1, tuning later.
# Amber upper bound matches the 1200 s stale threshold (D7, approved):
# beyond it a driver is no-signal territory, hence red.
AGE_GREEN_S = 300     # < 5 min
AGE_AMBER_S = 1200    # 5–20 min


def _check_key():
    secret = os.environ.get("CRON_SECRET", "")
    if secret and request.args.get("key", "") != secret:
        return False
    return True


@dashboard_bp.route("/team/dashboard", methods=["GET"])
def team_dashboard():
    if not _check_key():
        return jsonify({"error": "unauthorized"}), 401
    return render_template("team_dashboard.html",
                           key=request.args.get("key", ""))


def _bookings_today_tomorrow():
    """Slice 2: today+tomorrow bookings from booking_cache. Bookings with
    coordinates render as pins; the rest go to the no-coords list —
    honest about what can't render (no guessed positions)."""
    out = []
    try:
        # Dedicated connection (11 Aug — third cold-pool acquire timeout,
        # this time surfacing in the dashboard header): same proven
        # pattern as purge/status probes. ~2 short conns/min at the 30s
        # poll; keeps the board truthful even when the pool is sick.
        from db import _direct_conn
        now = datetime.now(ICT)
        days = [now.strftime("%Y-%m-%d"),
                (now + timedelta(days=1)).strftime("%Y-%m-%d")]
        with _direct_conn("dashboard-bookings") as conn:
            rows = conn.execute(
                "SELECT booking_id, tour_date, pickup_ts, status, "
                "type_of_package, pickup_lat, pickup_lng, geocode_precision, "
                "driver_id, payload->>'Name', payload->>'Last_Name', "
                "payload->>'Pickup_Location' "
                "FROM booking_cache WHERE tour_date = ANY(%s) "
                "ORDER BY pickup_ts NULLS LAST LIMIT 300",
                (days,),
            ).fetchall()
        for (bid, tour_date, pickup_ts, status, pkg, lat, lng, prec,
             driver_id, name, last_name, pickup_loc) in rows:
            pickup_time = ""
            try:
                if pickup_ts:
                    pickup_time = pickup_ts.astimezone(ICT).strftime("%H:%M")
            except Exception:
                pass
            out.append({
                "booking_id": bid,
                "name": f"{(name or '').strip()} {(last_name or '').strip()}".strip(),
                "tour_date": str(tour_date) if tour_date else None,
                "pickup_time": pickup_time,
                "status": (status or "").strip(),
                "type": (pkg or "").strip(),
                "lat": lat, "lng": lng,
                "precision": prec,
                "driver": driver_id,
                "pickup_location": (pickup_loc or "").strip()[:60],
            })
    except Exception as e:
        logger.error(f"[DASHBOARD] bookings query error: {e}")
    return out


@dashboard_bp.route("/team/dashboard/data", methods=["GET"])
def team_dashboard_data():
    if not _check_key():
        return jsonify({"error": "unauthorized"}), 401
    drivers = []
    try:
        from db import _direct_conn
        with _direct_conn("dashboard-drivers") as conn:
            rows = conn.execute(
                "SELECT driver_id, ts, lat, lng, speed, batt, updated_at "
                "FROM driver_latest ORDER BY updated_at DESC LIMIT 500"
            ).fetchall()
        # Display names from the in-memory registry (no Zoho reads)
        names = {}
        try:
            from gps_ingest import _refresh_providers, _provider_cache
            _refresh_providers()
            names = {c: e.get("name", "") for c, e in _provider_cache["by_code"].items()}
        except Exception:
            pass
        now = datetime.now(timezone.utc)
        for driver_id, ts, lat, lng, speed, batt, updated_at in rows:
            age_s = None
            try:
                age_s = int((now - updated_at).total_seconds())
            except Exception:
                pass
            drivers.append({
                "code": driver_id,
                "name": names.get(driver_id, ""),
                "lat": lat, "lng": lng,
                "speed": speed, "batt": batt,
                "age_s": age_s,
                "last_seen_ict": (updated_at.astimezone(ICT).strftime("%H:%M:%S")
                                  if updated_at else None),
            })
    except Exception as e:
        logger.error(f"[DASHBOARD] data error: {e}")
        return jsonify({"ok": False, "reason": str(e)[:120], "drivers": []}), 200
    return jsonify({"ok": True, "drivers": drivers,
                    "bookings": _bookings_today_tomorrow(),
                    "thresholds": {"green_s": AGE_GREEN_S, "amber_s": AGE_AMBER_S}}), 200
