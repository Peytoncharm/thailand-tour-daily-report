"""
gps_ingest.py
─────────────
Background GPS ingest for driver phones running Traccar Client
(OsmAnd HTTP protocol). Replaces the browser tracking page's
foreground-only pings with true background positions.

Blueprint: gps_bp
Endpoints:
  /gps/ingest/<secret>        — Traccar Client reports here (GET or POST)
  /gps/status/<code>?key=...  — ops debug: last ping for a provider code

Auth: GPS_INGEST_SECRET lives in the URL path (Traccar Client cannot
send headers). Device id must match an existing Provider_Code —
provider list cached in memory, refreshed hourly.

Position store is in-memory (ring buffer of ~200 points per code).
Render restarts wipe it — acceptable for V1, refills within a minute.

Env vars:
  GPS_INGEST_SECRET  — random path token for the ingest URL
  CRON_SECRET        — reused to protect the /gps/status debug route
"""

import logging
import os
import threading
from collections import deque
from datetime import datetime, timezone, timedelta

from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)

gps_bp = Blueprint("gps", __name__)

ICT = timezone(timedelta(hours=7))

GPS_INGEST_SECRET = os.environ.get("GPS_INGEST_SECRET", "")

RING_SIZE = 200
FLOOD_SECONDS = 10          # ignore pings more frequent than 1 per 10 s
PROVIDER_CACHE_TTL = 3600   # refresh provider-code list hourly
CACHE_MISS_RETRY_SECONDS = 60  # at most one forced refresh per minute
ZOHO_WRITEBACK_SECONDS = 300   # booking GPS fields at most every 5 min
BOOKING_LOOKUP_TTL = 300

# ─────────────────────────────────────────────────────────────
# In-memory state
# ─────────────────────────────────────────────────────────────

# Provider_Code (upper) → {"points": deque, "last_seen": dt(utc),
#                          "first_seen": dt(utc), "batt": float|None,
#                          "last_writeback": dt(utc)|None}
POSITIONS = {}

# Provider registry cache: code(upper) → {"id","name","code"}
_provider_cache = {"by_code": {}, "by_id": {}, "fetched_at": None,
                   "last_miss_refresh": None}
_cache_lock = threading.Lock()

# booking_id → (fetched_at utc, provider_code(upper)|None)
_booking_code_cache = {}


def _now_utc():
    return datetime.now(timezone.utc)


def _refresh_providers(force=False):
    """Refresh the Provider_Code registry from Zoho if stale (or forced)."""
    with _cache_lock:
        fetched = _provider_cache["fetched_at"]
        if not force and fetched and (_now_utc() - fetched).total_seconds() < PROVIDER_CACHE_TTL:
            return
        if force:
            last_miss = _provider_cache["last_miss_refresh"]
            if last_miss and (_now_utc() - last_miss).total_seconds() < CACHE_MISS_RETRY_SECONDS:
                return
            _provider_cache["last_miss_refresh"] = _now_utc()

    try:
        from zoho_thailand import zoho_get_records
        records = zoho_get_records(
            "Providers",
            fields="id,Name,Provider_Code,Outsourced_Agent",
            max_pages=10,
        )
    except Exception as e:
        logger.error(f"[GPS-INGEST] Provider refresh failed: {e}")
        return
    if not records:
        logger.warning("[GPS-INGEST] Provider refresh returned 0 records — keeping old cache")
        return

    by_code, by_id = {}, {}
    for r in records:
        code = (r.get("Provider_Code") or "").strip().upper()
        pid = r.get("id")
        if not code or not pid:
            continue
        entry = {"id": pid, "name": (r.get("Name") or "").strip(), "code": code}
        by_code[code] = entry
        by_id[pid] = entry

    with _cache_lock:
        _provider_cache["by_code"] = by_code
        _provider_cache["by_id"] = by_id
        _provider_cache["fetched_at"] = _now_utc()
    logger.info(f"[GPS-INGEST] Provider cache refreshed: {len(by_code)} codes")


def _lookup_code(code: str):
    """Resolve a device id to a cached provider entry.
    Cache miss → one forced refresh attempt, then reject."""
    code = (code or "").strip().upper()
    if not code:
        return None
    _refresh_providers()
    entry = _provider_cache["by_code"].get(code)
    if entry:
        return entry
    _refresh_providers(force=True)
    return _provider_cache["by_code"].get(code)


def _parse_timestamp(raw):
    """OsmAnd timestamp: epoch seconds or milliseconds. Fallback: now."""
    try:
        ts = float(raw)
        if ts > 1e12:
            ts = ts / 1000.0
        if ts > 0:
            return datetime.fromtimestamp(ts, tz=timezone.utc)
    except (TypeError, ValueError):
        pass
    return _now_utc()


def _writeback_worker(code: str, provider_id: str, lat: float, lng: float):
    """Update Approach_Last_GPS_* on this provider's Confirmed booking(s)
    with pickup today/tomorrow. Runs in a daemon thread — never blocks
    ingest; failures are logged and retried at the next throttle window."""
    try:
        from zoho_thailand import zoho_search, zoho_update_record
        now = datetime.now(ICT)
        fields = "id,Status,Provider_List,Pickup_Date_Time,Type_of_Package"
        matches = []
        for day in [now.strftime("%Y-%m-%d"),
                    (now + timedelta(days=1)).strftime("%Y-%m-%d")]:
            criteria = (
                "(Type_of_Package:equals:Private Transfer)"
                f"and(Tour_Date:equals:{day})"
            )
            for r in zoho_search("Koh_Chang_Orders", criteria, fields):
                if (r.get("Status") or "").strip() != "Confirmed":
                    continue
                prov = r.get("Provider_List")
                if isinstance(prov, dict) and prov.get("id") == provider_id:
                    matches.append(r["id"])

        now_str = now.strftime("%Y-%m-%dT%H:%M:%S+07:00")
        for bid in matches:
            zoho_update_record("Koh_Chang_Orders", bid, {
                "Approach_Last_GPS_Time": now_str,
                "Approach_Last_GPS_Lat": round(lat, 8),
                "Approach_Last_GPS_Lng": round(lng, 8),
            })
        if matches:
            logger.info(
                f"[GPS-INGEST] Zoho write-back {code}: {len(matches)} booking(s)"
            )
    except Exception as e:
        logger.error(f"[GPS-INGEST] Write-back error for {code}: {e}")


# ─────────────────────────────────────────────────────────────
# Public helpers for other modules
# ─────────────────────────────────────────────────────────────

def get_app_positions(code: str) -> list:
    """All buffered app positions for a provider code (oldest first)."""
    entry = POSITIONS.get((code or "").strip().upper())
    return list(entry["points"]) if entry else []


def get_last_seen(code: str):
    """UTC datetime of last accepted app ping, or None."""
    entry = POSITIONS.get((code or "").strip().upper())
    return entry["last_seen"] if entry else None


def has_tracked(code: str) -> bool:
    """True if this provider has sent app positions since last restart."""
    return (code or "").strip().upper() in POSITIONS


def code_for_provider_id(provider_id: str):
    """Provider_Code for a Zoho provider record id (from cache)."""
    if not provider_id:
        return None
    _refresh_providers()
    entry = _provider_cache["by_id"].get(provider_id)
    if entry:
        return entry["code"]
    _refresh_providers(force=True)
    entry = _provider_cache["by_id"].get(provider_id)
    return entry["code"] if entry else None


def code_for_booking(booking_id: str):
    """Provider_Code of the booking's assigned provider (cached 5 min)."""
    cached = _booking_code_cache.get(booking_id)
    if cached and (_now_utc() - cached[0]).total_seconds() < BOOKING_LOOKUP_TTL:
        return cached[1]
    code = None
    try:
        import requests
        from zoho_thailand import _get_access_token, ZOHO_API_BASE
        token = _get_access_token()
        if token:
            resp = requests.get(
                f"{ZOHO_API_BASE}/Koh_Chang_Orders/{booking_id}",
                headers={"Authorization": f"Zoho-oauthtoken {token}"},
                params={"fields": "Provider_List"},
                timeout=10,
            )
            if resp.status_code == 200:
                data = resp.json().get("data") or [{}]
                prov = data[0].get("Provider_List")
                if isinstance(prov, dict) and prov.get("id"):
                    code = code_for_provider_id(prov["id"])
    except Exception as e:
        logger.error(f"[GPS-INGEST] code_for_booking {booking_id} error: {e}")
    _booking_code_cache[booking_id] = (_now_utc(), code)
    return code


# ─────────────────────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────────────────────

@gps_bp.route("/gps/ingest/<secret>", methods=["GET", "POST"])
def gps_ingest(secret):
    """
    Traccar Client (OsmAnd protocol) reports here.
    Params: id, lat, lon, timestamp, speed, bearing, altitude,
    accuracy, batt — via query string (GET) or form body (POST).
    Always answers fast; never blocks on Zoho.
    """
    if not GPS_INGEST_SECRET or secret != GPS_INGEST_SECRET:
        logger.warning("[GPS-INGEST] Rejected: bad ingest secret")
        return "", 403

    params = request.args if request.args else request.form
    device_id = (params.get("id") or "").strip().upper()

    entry_meta = _lookup_code(device_id)
    if not entry_meta:
        logger.warning(f"[GPS-INGEST] Rejected unknown device id: {device_id!r}")
        return "", 403

    try:
        lat = float(params.get("lat"))
        lng = float(params.get("lon"))
    except (TypeError, ValueError):
        logger.warning(f"[GPS-INGEST] Bad lat/lon from {device_id}")
        return "", 200  # accept-and-drop: don't make the app retry garbage

    now = _now_utc()
    entry = POSITIONS.get(device_id)
    if entry and (now - entry["last_seen"]).total_seconds() < FLOOD_SECONDS:
        return "", 200  # flood guard: accept, ignore

    def _f(name):
        try:
            return float(params.get(name))
        except (TypeError, ValueError):
            return None

    point = {
        "lat": lat,
        "lng": lng,
        "ts": _parse_timestamp(params.get("timestamp")).isoformat(),
        "speed": _f("speed"),
        "bearing": _f("bearing"),
        "altitude": _f("altitude"),
        "accuracy": _f("accuracy"),
    }

    if entry is None:
        entry = {
            "points": deque(maxlen=RING_SIZE),
            "first_seen": now,
            "last_seen": now,
            "batt": None,
            "last_writeback": None,
        }
        POSITIONS[device_id] = entry
        logger.info(f"[GPS-INGEST] First ping from {device_id} ({entry_meta['name']})")

    entry["points"].append(point)
    entry["last_seen"] = now
    batt = _f("batt")
    if batt is not None:
        entry["batt"] = batt

    # Throttled Zoho write-back, off-thread
    last_wb = entry["last_writeback"]
    if last_wb is None or (now - last_wb).total_seconds() >= ZOHO_WRITEBACK_SECONDS:
        entry["last_writeback"] = now
        t = threading.Thread(
            target=_writeback_worker,
            args=(device_id, entry_meta["id"], lat, lng),
            daemon=True,
        )
        t.start()

    return "", 200


@gps_bp.route("/gps/status/<code>", methods=["GET"])
def gps_status(code):
    """Ops debug: last ping for a provider code. Protected by CRON_SECRET
    (checked here — the app-level gate only covers /cron|/admin|/test)."""
    cron_secret = os.environ.get("CRON_SECRET", "")
    if cron_secret and request.args.get("key", "") != cron_secret:
        return jsonify({"error": "unauthorized"}), 401

    code = (code or "").strip().upper()
    entry = POSITIONS.get(code)
    if not entry:
        return jsonify({
            "code": code,
            "tracking": False,
            "known_provider": _lookup_code(code) is not None,
        }), 200

    last = entry["points"][-1] if entry["points"] else None
    return jsonify({
        "code": code,
        "tracking": True,
        "points_buffered": len(entry["points"]),
        "first_seen_utc": entry["first_seen"].isoformat(),
        "last_seen_utc": entry["last_seen"].isoformat(),
        "last_seen_ict": entry["last_seen"].astimezone(ICT).strftime("%Y-%m-%d %H:%M:%S"),
        "batt": entry["batt"],
        "last_point": last,
        "last_writeback_utc": entry["last_writeback"].isoformat() if entry["last_writeback"] else None,
    }), 200
