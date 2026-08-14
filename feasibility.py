"""
feasibility.py — job-feasibility engine (Orathai GO, 13 Aug)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
ONE function answers "can the assigned driver physically reach the
pickup in time?" — used identically by the dashboard data endpoint,
the job board, and the alert cron, so all three always agree.

    slack = time_to_pickup − ETA(driver → pickup)

ETA tiers:
  drive legs   straight-line with speed tiers (35 km/h on-island,
               60 km/h for long mainland legs >80 km, else 45 — the
               Stage-2 D2 constant), upgraded by an eta_history
               route-key average when a matching zone→zone route with
               observed drive times exists
  ferry-aware  when driver side ≠ pickup side: drive to origin pier →
               NEXT actual sailing (timetable first/last/interval;
               past the last sailing rolls to tomorrow's first) →
               queue baseline → crossing → pier-to-pickup leg

Bands (route-aware per Orathai (a)):
  ok          slack ≥ +30 min (+60 when a ferry is involved — missing
              a sailing costs an hour, not minutes)
  at_risk     0 ≤ slack < band
  infeasible  slack < 0
  unknown     no assigned tracked driver, or GPS older than 20 min —
              feasibility NEVER implies knowledge it lacks (d)

Never raises; returns status "unknown" with a reason on any failure.
"""

import logging
import math
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

ICT = timezone(timedelta(hours=7))

GPS_FRESH_S = 1200          # 20 min — same threshold as the red signal tier
AT_RISK_MIN = 30
AT_RISK_MIN_FERRY = 60      # (a) route-aware band
ISLAND_SPEED = 35.0
HIGHWAY_SPEED = 60.0
DEFAULT_SPEED = 45.0        # Stage-2 D2 constant
HIGHWAY_KM = 80.0

_ISL = (11.90, 12.16, 102.20, 102.45)


def _on_island(lat, lng):
    return _ISL[0] <= lat <= _ISL[1] and _ISL[2] <= lng <= _ISL[3]


def _haversine_km(lat1, lng1, lat2, lng2):
    a, b, c, d = map(math.radians, (lat1, lng1, lat2, lng2))
    h = math.sin((c - a) / 2) ** 2 + math.cos(a) * math.cos(c) * math.sin((d - b) / 2) ** 2
    return 2 * 6371 * math.asin(math.sqrt(h))


def _drive_min(lat1, lng1, lat2, lng2):
    km = _haversine_km(lat1, lng1, lat2, lng2)
    both_island = _on_island(lat1, lng1) and _on_island(lat2, lng2)
    speed = ISLAND_SPEED if both_island else (HIGHWAY_SPEED if km > HIGHWAY_KM else DEFAULT_SPEED)
    return km / speed * 60 + 8  # +8 min start/park buffer


_route_cache = {"at": None, "avg": {}}


def _route_avg_min(from_zone, to_zone):
    """eta_history observed drive time for zone→zone, either direction.
    Cached 10 min. None when no data — geometric tier applies."""
    if not from_zone or not to_zone:
        return None
    now = datetime.now(timezone.utc)
    if _route_cache["at"] is None or (now - _route_cache["at"]).total_seconds() > 600:
        try:
            from db import _get_pool
            pool = _get_pool()
            if pool is not None:
                with pool.connection() as conn:
                    rows = conn.execute(
                        "SELECT route_key, AVG(COALESCE(actual_sec, predicted_sec)) "
                        "FROM eta_history WHERE route_key IS NOT NULL "
                        "AND route_key NOT LIKE 'ferry-queue%%' "
                        "AND COALESCE(actual_sec, predicted_sec) > 0 "
                        "AND computed_at > now() - interval '60 days' "
                        "GROUP BY route_key").fetchall()
                _route_cache["avg"] = {r[0]: float(r[1]) / 60 for r in rows if r[1]}
                _route_cache["at"] = now
        except Exception as e:
            logger.warning(f"[FEAS] route cache failed: {e}")
            _route_cache["at"] = now
    avg = _route_cache["avg"]
    for k in (f"{from_zone}->{to_zone}", f"{to_zone}->{from_zone}"):
        if k in avg:
            return avg[k]
    return None


def _nearest_zone(lat, lng, max_km=6):
    try:
        from pickup_matcher import _load_points
        best, best_km = None, max_km
        for name, p in _load_points().items():
            km = _haversine_km(lat, lng, p["lat"], p["lng"])
            if km < best_km:
                best, best_km = name, km
        return best
    except Exception:
        return None


def _ferry(m_key):
    try:
        from ferry_model import load_model
        return load_model()
    except Exception:
        return {}


def compute(pickup_dt, pickup_lat, pickup_lng, pickup_zone,
            driver_lat, driver_lng, gps_age_s, now=None):
    """Core feasibility. All-None-safe. Returns dict:
    {status, slack_min, eta, band_min, ferry_involved, detail}"""
    now = now or datetime.now(ICT)
    if pickup_dt is None or pickup_lat is None or pickup_lng is None:
        return {"status": "unknown", "detail": "no pickup coordinates"}
    if driver_lat is None or driver_lng is None:
        return {"status": "unknown", "detail": "no GPS to judge"}
    if gps_age_s is None or gps_age_s > GPS_FRESH_S:
        return {"status": "unknown", "detail": "GPS stale — no fresh position to judge"}
    try:
        from ferry_model import ISLAND_ZONES, load_model
        m = load_model()
    except Exception:
        ISLAND_ZONES, m = set(), {}

    pickup_island = ((pickup_zone or "").lower() in ISLAND_ZONES
                     or _on_island(pickup_lat, pickup_lng))
    driver_island = _on_island(driver_lat, driver_lng)
    ferry_involved = pickup_island != driver_island

    detail_bits = []
    if not ferry_involved:
        drv_zone = _nearest_zone(driver_lat, driver_lng)
        rt = _route_avg_min(drv_zone, (pickup_zone or "").lower())
        if rt is not None:
            eta_min = rt + 8
            detail_bits.append(f"route-observed ~{int(rt)}m")
        else:
            eta_min = _drive_min(driver_lat, driver_lng, pickup_lat, pickup_lng)
            detail_bits.append(f"drive ~{int(eta_min)}m (geometric)")
        eta_dt = now + timedelta(minutes=eta_min)
    else:
        try:
            from pickup_matcher import _load_points
            pts = _load_points()
        except Exception:
            pts = {}
        origin_pier = m.get("pier_island" if driver_island else "pier_mainland",
                            "ao sapparot pier" if driver_island else "ao thammachat pier")
        dest_pier = m.get("pier_island" if pickup_island else "pier_mainland",
                          "ao sapparot pier" if pickup_island else "ao thammachat pier")
        op, dp = pts.get(origin_pier), pts.get(dest_pier)
        if not op or not dp:
            return {"status": "unknown", "detail": "ferry pins missing"}
        to_pier = _drive_min(driver_lat, driver_lng, op["lat"], op["lng"])
        pier_arrive = now + timedelta(minutes=to_pier)
        # Verified timetable (14 Aug): next sailing is a LIST LOOKUP with
        # the 45-min pre-boarding rule (cash-only counter, no pre-purchase)
        from ferry_model import next_departure
        dep = next_departure(pier_arrive, m)
        if dep is None:
            return {"status": "unknown", "detail": "no catchable sailing found"}
        crossing = int(m.get("crossing_min", 30))
        leg = _drive_min(dp["lat"], dp["lng"], pickup_lat, pickup_lng)
        eta_dt = dep + timedelta(minutes=crossing + leg)
        pre = int(m.get("pre_boarding_min", 45))
        detail_bits.append(
            f"pier ~{int(to_pier)}m → ferry {dep.strftime('%H:%M' if dep.date() == now.date() else '%d/%m %H:%M')} "
            f"(ถึงท่าก่อน {pre}น.) +cross {crossing}m +drive ~{int(leg)}m")

    slack_min = int((pickup_dt - eta_dt).total_seconds() // 60)
    band = AT_RISK_MIN_FERRY if ferry_involved else AT_RISK_MIN
    if slack_min < 0:
        status = "infeasible"
    elif slack_min < band:
        status = "at_risk"
    else:
        status = "ok"
    return {"status": status, "slack_min": slack_min,
            "eta": eta_dt.strftime("%H:%M"), "band_min": band,
            "ferry_involved": ferry_involved,
            "detail": " · ".join(detail_bits)}


def for_cached_booking(row_payload, pickup_ts, driver_code, now=None):
    """Adapter for booking_cache rows + driver_latest. Never raises."""
    try:
        from db import _get_pool
        pool = _get_pool()
        pos = None
        if pool is not None and driver_code:
            with pool.connection() as conn:
                pos = conn.execute(
                    "SELECT ts, lat, lng FROM driver_latest WHERE driver_id = %s",
                    ((driver_code or "").upper(),)).fetchone()
        gps_age = ((datetime.now(timezone.utc) - pos[0]).total_seconds()
                   if pos else None)
        p = row_payload or {}
        lat = p.get("Pickup_Lat")
        lng = p.get("Pickup_Lng")
        return compute(
            pickup_ts.astimezone(ICT) if pickup_ts else None,
            float(lat) if lat is not None else None,
            float(lng) if lng is not None else None,
            (p.get("Pickup_Zone") or "").lower(),
            pos[1] if pos else None, pos[2] if pos else None,
            gps_age, now=now)
    except Exception as e:
        logger.warning(f"[FEAS] adapter failed: {e}")
        return {"status": "unknown", "detail": "feasibility error"}
