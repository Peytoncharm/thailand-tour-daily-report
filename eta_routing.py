"""
eta_routing.py — Stage 2 Step 4: stage-2 paid routing check (SHADOW, D1)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Only jobs that FAIL the free stage-1 filter (at-risk or ferry-needed)
reach this module. Provider-agnostic behind get_route_eta(); the D1
decision is Google, implemented against the Routes API v2. The key
lives in GOOGLE_ROUTING_API_KEY — while it is unset every call is a
clean skip (counted in the cron JSON as skipped_no_key), so this whole
step deploys dark and lights up the moment billing + key exist, with
zero code change.

Ferry segmentation: when driver and pickup are on opposite sides,
  total = road(driver→pier) + queue_baseline + crossing + road(pier→pickup)
with queue/crossing from ferry_model.json (method='road+ferry').

Per-route correction (spec proposed defaults): corrected = predicted ×
median(actual/predicted) over the last N=10 completions on this
route_key, applied only when ≥5 samples exist.

Cost control: ROUTING_DAILY_CAP (default 200) — at the cap, calls are
skipped and counted; the daily call count is derived from eta_history
rows (method road/road+ferry today), no extra state.

SHADOW: results are eta_history rows only. Nothing is sent, nothing
reads these rows yet.
"""

import logging
import os
from statistics import median

logger = logging.getLogger(__name__)

ROUTING_DAILY_CAP = int(os.environ.get("ROUTING_DAILY_CAP", "200"))

_GOOGLE_URL = "https://routes.googleapis.com/directions/v2:computeRoutes"

_warned_no_key = False


def _api_key():
    """Orathai created the key as GOOGLE_MAPS_API_KEY (restricted to
    Geocoding + Routes — the same key serves the geocode-upgrade queue
    later). GOOGLE_ROUTING_API_KEY kept as a fallback name."""
    return (os.environ.get("GOOGLE_MAPS_API_KEY", "").strip()
            or os.environ.get("GOOGLE_ROUTING_API_KEY", "").strip())


def get_route_eta(from_latlng, to_latlng):
    """One road-routing call: {'sec': int, 'km': float} or None.
    Provider per D1: Google Routes API v2. No key -> None (skip)."""
    global _warned_no_key
    key = _api_key()
    if not key:
        if not _warned_no_key:
            logger.info("[STAGE2] GOOGLE_MAPS_API_KEY not set — stage-2 "
                        "calls skip cleanly until billing/key exist (D1)")
            _warned_no_key = True
        return None
    try:
        import requests
        r = requests.post(
            _GOOGLE_URL,
            headers={
                "Content-Type": "application/json",
                "X-Goog-Api-Key": key,
                "X-Goog-FieldMask": "routes.duration,routes.distanceMeters",
            },
            json={
                "origin": {"location": {"latLng": {
                    "latitude": from_latlng[0], "longitude": from_latlng[1]}}},
                "destination": {"location": {"latLng": {
                    "latitude": to_latlng[0], "longitude": to_latlng[1]}}},
                "travelMode": "DRIVE",
                "routingPreference": "TRAFFIC_AWARE",
            },
            timeout=10,
        )
        if r.status_code != 200:
            logger.warning(f"[STAGE2] routing HTTP {r.status_code}: {r.text[:150]}")
            return None
        routes = (r.json() or {}).get("routes") or []
        if not routes:
            return None
        dur = routes[0].get("duration", "0s")
        sec = int(float(str(dur).rstrip("s")))
        return {"sec": sec, "km": round(routes[0].get("distanceMeters", 0) / 1000.0, 2)}
    except Exception as e:
        logger.warning(f"[STAGE2] routing call failed: {e}")
        return None


def _calls_today(conn):
    row = conn.execute(
        "SELECT count(*) FROM eta_history WHERE method IN ('road', 'road+ferry') "
        "AND computed_at::date = (now() AT TIME ZONE 'Asia/Bangkok')::date"
    ).fetchone()
    return int(row[0])


def _correction(conn, route_key):
    """median(actual/predicted) over last 10 completions on this route,
    applied only with >=5 samples (spec proposed). 1.0 otherwise."""
    if not route_key:
        return 1.0
    rows = conn.execute(
        "SELECT actual_sec::float / predicted_sec FROM eta_history "
        "WHERE route_key = %s AND actual_sec IS NOT NULL "
        "AND predicted_sec IS NOT NULL AND predicted_sec > 0 "
        "ORDER BY computed_at DESC LIMIT 10", (route_key,)).fetchall()
    ratios = [r[0] for r in rows]
    return round(median(ratios), 3) if len(ratios) >= 5 else 1.0


def run_stage2(b, win, dpos, pin, remaining_sec, route_key, drv_code, counters):
    """Stage-2 check for one at-risk/ferry-needed booking. Writes ONE new
    eta_history row (method 'road' or 'road+ferry'); returns nothing the
    caller must act on — shadow. Never raises."""
    try:
        from db import _get_pool
        from ferry_model import load_model, _pier_pins
        pool = _get_pool()
        if pool is None or dpos is None or pin is None:
            return
        with pool.connection() as conn:
            if _calls_today(conn) >= ROUTING_DAILY_CAP:
                counters["capped"] += 1
                return

            model = load_model()
            main_pin, isl_pin = _pier_pins()
            from eta_checkpoints import _on_island
            drv_isl, pk_isl = _on_island(*dpos), _on_island(*pin)

            if drv_isl != pk_isl and main_pin and isl_pin:
                near, far = (isl_pin, main_pin) if drv_isl else (main_pin, isl_pin)
                leg1 = get_route_eta(dpos, near)
                leg2 = get_route_eta(far, pin)
                if leg1 is None or leg2 is None:
                    counters["skipped_no_key" if not _api_key()
                             else "provider_error"] += 1
                    return
                queue_sec = int(model.get("queue_min_baseline", {})
                                .get("default", 15)) * 60
                crossing_sec = int(model.get("crossing_min", 30)) * 60
                sec = leg1["sec"] + queue_sec + crossing_sec + leg2["sec"]
                km = leg1["km"] + leg2["km"]
                method = "road+ferry"
            else:
                res = get_route_eta(dpos, pin)
                if res is None:
                    counters["skipped_no_key" if not _api_key()
                             else "provider_error"] += 1
                    return
                sec, km, method = res["sec"], res["km"], "road"

            corr = _correction(conn, route_key)
            corrected = int(sec * corr)
            conn.execute(
                "INSERT INTO eta_history (booking_id, driver_id, route_key, "
                "distance_km, predicted_sec, method) VALUES (%s, %s, %s, %s, %s, %s)",
                (str(b.get("id")), drv_code, route_key, km,
                 corrected, method))
            counters[method.replace("+", "_")] += 1
            at_risk = remaining_sec < corrected + 600  # 10-min buffer [spec]
            logger.info(f"[STAGE2] booking={b.get('id')} window={win} "
                        f"method={method} raw={sec}s corr×{corr}={corrected}s "
                        f"remaining={remaining_sec}s -> "
                        f"{'AT-RISK' if at_risk else 'ok'} (shadow)")
    except Exception as e:
        logger.warning(f"[STAGE2] failed for {b.get('id')}: {e}")
