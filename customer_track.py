"""
customer_track.py
─────────────────
Read-only live map for the passenger. English UI (foreign tourists).

Blueprint: customer_bp
Endpoints:
  /customer/track/<booking_id>/<token>        — page
  /customer/track/<booking_id>/<token>/data   — poll JSON

Token: HMAC-SHA256 of booking_id with CUSTOMER_LINK_SECRET
(first 20 hex chars) — prevents guessing other bookings, no login.

Privacy: the page reveals only this one job — driver first name,
vehicle model/colour/registration, driver phone, pickup time and
position. Never bank/cost fields or other bookings.

Env vars:
  CUSTOMER_LINK_SECRET — HMAC key for link tokens
"""

import hashlib
import hmac
import logging
import os
from datetime import datetime, timezone, timedelta

import requests
from flask import Blueprint, jsonify, render_template

logger = logging.getLogger(__name__)

customer_bp = Blueprint("customer", __name__)

ICT = timezone(timedelta(hours=7))

CUSTOMER_LINK_SECRET = os.environ.get("CUSTOMER_LINK_SECRET", "")
BASE_URL = "https://thailand-tour-daily-report.onrender.com"

TOKEN_LEN = 20
INFO_CACHE_TTL = 300

# booking_id → (fetched_at utc, info dict)
_info_cache = {}


def customer_token(booking_id: str) -> str:
    """Deterministic link token for a booking."""
    if not CUSTOMER_LINK_SECRET:
        return ""
    digest = hmac.new(
        CUSTOMER_LINK_SECRET.encode(), str(booking_id).encode(), hashlib.sha256
    ).hexdigest()
    return digest[:TOKEN_LEN]


def customer_link(booking_id: str) -> str:
    """Full customer tracking URL (used by the email builder)."""
    return f"{BASE_URL}/customer/track/{booking_id}/{customer_token(booking_id)}"


def _token_ok(booking_id: str, token: str) -> bool:
    expected = customer_token(booking_id)
    if not expected or not token:
        return False
    return hmac.compare_digest(expected, token)


def _fetch_info(booking_id: str) -> dict:
    """Booking + provider card, cached 5 min. Only customer-safe fields."""
    cached = _info_cache.get(booking_id)
    now = datetime.now(timezone.utc)
    if cached and (now - cached[0]).total_seconds() < INFO_CACHE_TTL:
        return cached[1]

    info = {
        "found": False, "customer_first": "", "pickup_time": "",
        "pickup_date": "", "pickup_location": "", "tour_date": "",
        "status": "", "provider_id": None, "provider_code": None,
        "driver_first": "", "car_model": "", "car_colour": "",
        "car_registration": "", "driver_phone": "",
    }
    try:
        from zoho_thailand import _get_access_token, ZOHO_API_BASE
        token = _get_access_token()
        if not token:
            return info
        resp = requests.get(
            f"{ZOHO_API_BASE}/Koh_Chang_Orders/{booking_id}",
            headers={"Authorization": f"Zoho-oauthtoken {token}"},
            params={"fields": "Name,Pickup_Date_Time,Tour_Date,Pickup_Location,"
                    "Provider_List,Status"},
            timeout=10,
        )
        if resp.status_code != 200:
            return info
        b = (resp.json().get("data") or [{}])[0]
        info["found"] = True
        info["customer_first"] = ((b.get("Name") or "").strip().split(" ") or [""])[0]
        info["tour_date"] = (b.get("Tour_Date") or "").split("T")[0]
        info["pickup_location"] = (b.get("Pickup_Location") or "").strip()
        info["status"] = (b.get("Status") or "").strip()
        pdt = b.get("Pickup_Date_Time") or ""
        if pdt and "T" in pdt:
            info["pickup_time"] = pdt.split("T")[1][:5]
            info["pickup_date"] = pdt.split("T")[0]

        prov = b.get("Provider_List")
        if isinstance(prov, dict) and prov.get("id"):
            info["provider_id"] = prov["id"]
            resp2 = requests.get(
                f"{ZOHO_API_BASE}/Providers/{prov['id']}",
                headers={"Authorization": f"Zoho-oauthtoken {token}"},
                params={"fields": "Name,Provider_Code,Phone_1,Car_Model,"
                        "Car_Colour,Vehicle_Registration"},
                timeout=10,
            )
            if resp2.status_code == 200:
                p = (resp2.json().get("data") or [{}])[0]
                info["driver_first"] = ((p.get("Name") or "").strip().split(" ") or [""])[0]
                info["provider_code"] = (p.get("Provider_Code") or "").strip().upper() or None
                info["car_model"] = (p.get("Car_Model") or "").strip()
                info["car_colour"] = (p.get("Car_Colour") or "").strip()
                info["car_registration"] = (p.get("Vehicle_Registration") or "").strip()
                info["driver_phone"] = (p.get("Phone_1") or "").strip()
    except Exception as e:
        logger.error(f"[CUSTOMER-TRACK] Info fetch error {booking_id}: {e}")

    _info_cache[booking_id] = (now, info)
    return info


def _trip_state(info: dict) -> str:
    """'completed' | 'assigning' | 'live'"""
    tour_date = info.get("tour_date")
    if tour_date:
        try:
            td = datetime.strptime(tour_date, "%Y-%m-%d").date()
            if td < datetime.now(ICT).date():
                return "completed"
        except ValueError:
            pass
    if not info.get("provider_id"):
        return "assigning"
    return "live"


@customer_bp.route("/customer/track/<booking_id>/<token>", methods=["GET"])
def customer_track_page(booking_id, token):
    if not _token_ok(booking_id, token):
        logger.warning(f"[CUSTOMER-TRACK] Bad token for {booking_id}")
        return render_template("customer_track.html", state="forbidden",
                               booking_id="", token="", info={}), 403

    info = _fetch_info(booking_id)
    if not info.get("found"):
        return render_template("customer_track.html", state="forbidden",
                               booking_id="", token="", info={}), 404

    return render_template(
        "customer_track.html",
        state=_trip_state(info),
        booking_id=booking_id,
        token=token,
        info=info,
    )


@customer_bp.route("/customer/track/<booking_id>/<token>/data", methods=["GET"])
def customer_track_data(booking_id, token):
    """Position JSON polled by the customer page.
    Prefers app-store positions; falls back to browser-page pings."""
    if not _token_ok(booking_id, token):
        return jsonify({"error": "forbidden"}), 403

    info = _fetch_info(booking_id)
    state = _trip_state(info)
    if state != "live":
        return jsonify({"status": state}), 200

    # 1) App positions (background tracker) — preferred
    from gps_ingest import get_app_positions
    code = info.get("provider_code")
    if code:
        points = get_app_positions(code)
        if points:
            last = points[-1]
            last_dt = datetime.fromisoformat(last["ts"])
            return jsonify({
                "status": "ok",
                "source": "app",
                "lat": last["lat"],
                "lng": last["lng"],
                "trail": [[p["lat"], p["lng"]] for p in points],
                "updated_at": last_dt.astimezone(ICT).strftime("%Y-%m-%dT%H:%M:%S"),
            }), 200

    # 2) Browser tracking session fallback
    from driver_location import tracking_sessions
    session = tracking_sessions.get(booking_id)
    if session and session.get("lat") is not None:
        return jsonify({
            "status": "ok",
            "source": "browser",
            "lat": session["lat"],
            "lng": session["lng"],
            "trail": [],
            "updated_at": session.get("updated_at"),
        }), 200

    return jsonify({"status": "waiting"}), 200
