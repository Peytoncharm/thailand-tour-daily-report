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


# ─────────────────────────────────────────────────────────────
# DEMO MODE (11 Aug): ?demo=1 overlays SIMULATED data generated from
# the real zone table (bundled CSV — zero DB reads, zero writes
# anywhere). Deterministic seed -> stable board across 30s refreshes.
# Driver codes D-xx (real codes are P####) so nothing can be mistaken.
# ─────────────────────────────────────────────────────────────

_DEMO_ZONE_WEIGHTS = [
    ("kai bae", 6), ("klong prao", 4), ("white sand beach", 3),
    ("lonely beach", 3), ("bailan beach", 1), ("klong son", 1),
    ("bang bao pier", 2), ("salak phet", 1),
    ("ao sapparot pier", 2), ("ao thammachat pier", 2),
    ("laem ngop pier", 1), ("trat town", 2), ("trat airport", 2),
    ("laem sok pier", 1), ("chanthaburi", 1), ("rayong", 1),
    ("ban phe pier", 1), ("pattaya", 2), ("suvarnabhumi", 3),
    ("don mueang", 1), ("mo chit bus terminal", 1),
    ("ekkamai bus terminal", 1),
]
# Demo-realism (11 Aug): plain radial jitter around coastal zone centres
# was dropping simulated drivers into the sea. Known-coastal zones get an
# inland unit-direction (away from the water); scatter runs along it plus
# a small along-shore wobble. No coastline data — a rough bearing per
# zone is enough at these distances. Deterministic seed unchanged.
_DEMO_INLAND_BIAS = {
    # Koh Chang west-coast beaches: sea to the west -> inland = east
    "white sand beach": (0.0, 1.0),
    "klong prao": (0.0, 1.0),
    "kai bae": (0.0, 1.0),
    "lonely beach": (0.0, 1.0),
    "bailan beach": (0.3, 1.0),
    "klong son": (-0.3, 1.0),        # bay opens north-west -> inland = SE
    "bang bao pier": (1.0, 0.3),     # south coast -> inland = north
    "salak phet": (1.0, -0.3),       # south-east bay -> inland = NW
    "ao sapparot pier": (-1.0, 0.2), # north-tip pier -> inland = south
    # mainland coast: sea to the south/south-west
    "ao thammachat pier": (0.7, 0.7),
    "laem ngop pier": (0.7, 0.7),
    "laem sok pier": (0.7, 0.7),
    "ban phe pier": (1.0, 0.0),
    "pattaya": (0.0, 1.0),           # sea west -> inland = east
}

def _demo_scatter(rng, zone, p, along_max, perp_max):
    """One scatter position near a zone centre. Coastal zones scatter
    only inland (plus along-shore wobble); inland zones keep the old
    symmetric jitter at the same scale."""
    bias = _DEMO_INLAND_BIAS.get(zone)
    if not bias:
        return (p["lat"] + rng.uniform(-along_max, along_max),
                p["lng"] + rng.uniform(-along_max, along_max))
    blat, blng = bias
    n = (blat * blat + blng * blng) ** 0.5
    blat, blng = blat / n, blng / n
    along = rng.uniform(0.15 * along_max, along_max)   # strictly inland
    perp = rng.uniform(-perp_max, perp_max)            # along-shore wobble
    return (p["lat"] + blat * along - blng * perp,
            p["lng"] + blng * along + blat * perp)

_DEMO_NAMES = ["Anna K", "Grzegorz W", "Tom H", "Lena M", "Marco P",
               "Sophie B", "James T", "Nina R", "Oliver S", "Emma L",
               "Lukas F", "Marie C", "David N", "Julia W", "Chris O",
               "Petra Z", "Sam Y", "Ida Q", "Noah V", "Mia D",
               "Erik J", "Clara G", "Ben A", "Zoe E", "Felix U"]

def _demo_payload():
    import random
    from pickup_matcher import _load_points
    pts = _load_points()
    rng = random.Random(20260811)   # fixed seed -> stable board

    zone_pool = []
    for z, w in _DEMO_ZONE_WEIGHTS:
        if z in pts:
            zone_pool.extend([z] * w)

    drivers = []
    n_drivers = 32
    for i in range(n_drivers):
        z = zone_pool[rng.randrange(len(zone_pool))]
        p = pts[z]
        lat, lng = _demo_scatter(rng, z, p, 0.010, 0.005)
        r = rng.random()
        if r < 0.60:
            age = rng.randint(20, 280)
        elif r < 0.80:
            age = rng.randint(400, 1100)
        elif r < 0.86:
            age = rng.randint(1500, 4000)
        else:
            age = None   # grey idle
        drivers.append({
            "code": f"D-{i+1:02d}", "name": f"คนขับสาธิต {i+1:02d}",
            "lat": round(lat, 6), "lng": round(lng, 6),
            "speed": None, "batt": rng.randint(35, 98),
            "age_s": age,
            "last_seen_ict": None if age is None else
                datetime.now(ICT).strftime("%H:%M:%S"),
            # Obviously fake number; the templates also disable all
            # tel:/wa.me links in DEMO mode.
            "phone": f"08x-xxx-{1000 + i:04d}",
        })

    bookings = []
    now = datetime.now(ICT)
    n_book = 20
    for i in range(n_book):
        z = zone_pool[rng.randrange(len(zone_pool))]
        p = pts[z]
        day = now if rng.random() < 0.55 else now + timedelta(days=1)
        hh = rng.choice([6, 7, 8, 8, 9, 9, 10, 11, 13, 15, 17, 18])
        status = rng.choice(["Confirmed"] * 7 + ["", "Pending", ""])
        typ = rng.choice(["Private Transfer"] * 5 + ["Join Transfer"] * 2
                         + ["Activity Tour"] * 3)
        dz = zone_pool[rng.randrange(len(zone_pool))]
        drv = None if rng.random() < 0.15 else f"D-{rng.randint(1, n_drivers):02d}"
        blat, blng = _demo_scatter(rng, z, p, 0.005, 0.003)
        bookings.append({
            "booking_id": f"DEMO-{i+1:03d}",
            "name": _DEMO_NAMES[i % len(_DEMO_NAMES)],
            "tour_date": day.strftime("%Y-%m-%d"),
            "pickup_time": f"{hh:02d}:{rng.choice(['00','15','30','45'])}",
            "status": status, "type": typ,
            "lat": round(blat, 6),
            "lng": round(blng, 6),
            "precision": p["precision"],
            "driver": drv,
            "pickup_location": z.title(),
            "dropoff_location": dz.title(),
            "customer_phone": f"08x-xxx-{2000 + i:04d}",
            "driver_phone": None if drv is None else f"08x-xxx-{3000 + i:04d}",
        })
    # honest no-coords entries
    for i, txt in enumerate(["Sunset Villa (address unclear)",
                             "Meet at 7-11 near the temple",
                             "Blue House Homestay"]):
        bookings.append({
            "booking_id": f"DEMO-NC-{i+1}", "name": _DEMO_NAMES[-(i+1)],
            "tour_date": now.strftime("%Y-%m-%d"),
            "pickup_time": f"{rng.randint(8, 17):02d}:30",
            "status": "Confirmed", "type": "Private Transfer",
            "lat": None, "lng": None, "precision": None,
            "driver": None, "pickup_location": txt,
            "dropoff_location": "Suvarnabhumi",
        })

    return {"ok": True, "demo": True, "drivers": drivers,
            "bookings": bookings,
            "thresholds": {"green_s": AGE_GREEN_S, "amber_s": AGE_AMBER_S}}


@dashboard_bp.route("/team/dashboard", methods=["GET"])
def team_dashboard():
    if not _check_key():
        return jsonify({"error": "unauthorized"}), 401
    return render_template("team_dashboard.html",
                           key=request.args.get("key", ""),
                           demo=(request.args.get("demo") == "1"))


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
                "driver_id, provider_id, payload->>'Name', payload->>'Last_Name', "
                "payload->>'Pickup_Location', payload->>'Dropoff_Location', "
                "payload->>'Phone_WhatsApp', payload->>'Phone' "
                "FROM booking_cache WHERE tour_date = ANY(%s) "
                "ORDER BY pickup_ts NULLS LAST LIMIT 300",
                (days,),
            ).fetchall()
        for (bid, tour_date, pickup_ts, status, pkg, lat, lng, prec,
             driver_id, provider_id, name, last_name, pickup_loc, dropoff_loc,
             ph_wa, ph) in rows:
            drv_phone = None
            if provider_id:
                try:
                    from gps_ingest import provider_entry_for_id
                    e = provider_entry_for_id(provider_id)
                    drv_phone = (e or {}).get("phone") or None
                except Exception:
                    pass
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
                "dropoff_location": (dropoff_loc or "").strip()[:60],
                "provider_id": provider_id,
                "customer_phone": ((ph_wa or ph) or "").strip() or None,
                "driver_phone": drv_phone,
            })
    except Exception as e:
        logger.error(f"[DASHBOARD] bookings query error: {e}")
    return out


@dashboard_bp.route("/team/board", methods=["GET"])
def team_board():
    """List-view companion: the map answers 'where', this answers 'what
    and how many'. Same key gate, same data endpoint, demo=1 supported."""
    if not _check_key():
        return jsonify({"error": "unauthorized"}), 401
    return render_template("team_board.html",
                           key=request.args.get("key", ""),
                           demo=(request.args.get("demo") == "1"))


@dashboard_bp.route("/team/dashboard/data", methods=["GET"])
def team_dashboard_data():
    if not _check_key():
        return jsonify({"error": "unauthorized"}), 401
    if request.args.get("demo") == "1":
        return jsonify(_demo_payload()), 200
    drivers = []
    try:
        from db import _direct_conn
        with _direct_conn("dashboard-drivers") as conn:
            rows = conn.execute(
                "SELECT driver_id, ts, lat, lng, speed, batt, updated_at "
                "FROM driver_latest ORDER BY updated_at DESC LIMIT 500"
            ).fetchall()
        # Display names + phones from the in-memory registry (no Zoho reads)
        names, phones = {}, {}
        try:
            from gps_ingest import _refresh_providers, _provider_cache
            _refresh_providers()
            for c, e in _provider_cache["by_code"].items():
                names[c] = e.get("name", "")
                phones[c] = e.get("phone", "") or None
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
                "phone": phones.get(driver_id),
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
