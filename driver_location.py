import os
import logging
import threading
import requests
from datetime import datetime, timezone, timedelta
from flask import Blueprint, request, jsonify, render_template

logger = logging.getLogger(__name__)

driver_bp = Blueprint("driver", __name__)

ICT = timezone(timedelta(hours=7))

TRANSFER_LINE_TOKEN = os.environ.get("TRANSFER_LINE_TOKEN", "")
BASE_URL = "https://thailand-tour-daily-report.onrender.com"

# In-memory tracking sessions keyed by booking_id (Zoho record ID)
# {booking_id: {lat, lng, accuracy, updated_at, started_at,
#               page_opened_at, customer_name, pickup_location, pickup_time,
#               line_user_id, active, notified, watchdog_fired}}
tracking_sessions = {}

SESSION_TTL_HOURS = 8


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cleanup_stale():
    """Remove sessions older than SESSION_TTL_HOURS."""
    cutoff = datetime.now(ICT) - timedelta(hours=SESSION_TTL_HOURS)
    stale = [
        k for k, v in tracking_sessions.items()
        if v.get("page_opened_at") and v["page_opened_at"] < cutoff
    ]
    for k in stale:
        del tracking_sessions[k]
    if stale:
        logger.info(f"[DRIVER-TRACK] Cleaned up {len(stale)} stale sessions")


def _line_push(to, text):
    """Send a LINE push message via TRANSFER_LINE_TOKEN. Returns True on success."""
    if not TRANSFER_LINE_TOKEN or not to:
        return False
    try:
        resp = requests.post(
            "https://api.line.me/v2/bot/message/push",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {TRANSFER_LINE_TOKEN}",
            },
            json={"to": to, "messages": [{"type": "text", "text": text}]},
            timeout=10,
        )
        if resp.status_code == 200:
            logger.info(f"[DRIVER-TRACK] LINE push OK to {to[:10]}...")
            return True
        else:
            logger.error(f"[DRIVER-TRACK] LINE push failed {resp.status_code}: {resp.text}")
            return False
    except Exception as e:
        logger.error(f"[DRIVER-TRACK] LINE push exception: {e}")
        return False


def _update_zoho_approach_gps(booking_id, lat, lng, is_first_ping=False):
    """Update Approach GPS fields on the Zoho booking. Best-effort, non-blocking."""
    try:
        from zoho_thailand import _get_access_token, ZOHO_API_BASE
        token = _get_access_token()
        if not token:
            return
        now_str = datetime.now(ICT).strftime("%Y-%m-%dT%H:%M:%S+07:00")
        fields = {
            "Approach_Last_GPS_Time": now_str,
            "Approach_Last_GPS_Lat": round(lat, 8),
            "Approach_Last_GPS_Lng": round(lng, 8),
        }
        if is_first_ping:
            fields["Approach_Link_Opened"] = "Yes"
        requests.put(
            f"{ZOHO_API_BASE}/Koh_Chang_Orders/{booking_id}",
            headers={"Authorization": f"Zoho-oauthtoken {token}", "Content-Type": "application/json"},
            json={"data": [fields], "trigger": []},
            timeout=10,
        )
        logger.info(f"[APPROACH] Zoho GPS updated: booking={booking_id}, lat={lat:.6f}, lng={lng:.6f}")
    except Exception as e:
        logger.error(f"[APPROACH] Zoho GPS update error: {e}")


def _fetch_booking_and_provider(booking_id):
    """Look up booking from Zoho, then fetch provider Line_User_ID.
    Returns (customer_name, pickup_time, line_user_id, pickup_datetime_iso) or (None, None, None, None).
    """
    try:
        from zoho_thailand import _get_access_token, ZOHO_API_BASE
        token = _get_access_token()
        if not token:
            return None, None, None, None

        # Get booking
        resp = requests.get(
            f"{ZOHO_API_BASE}/Koh_Chang_Orders/{booking_id}",
            headers={"Authorization": f"Zoho-oauthtoken {token}"},
            params={"fields": "Name,Last_Name,Pickup_Date_Time,Pickup_Time,Provider_List"},
            timeout=10,
        )
        if resp.status_code != 200:
            logger.warning(f"[DRIVER-TRACK] Booking {booking_id} fetch failed: {resp.status_code}")
            return None, None, None, None

        booking = resp.json().get("data", [{}])[0]
        name = (booking.get("Name") or "").strip()
        last = (booking.get("Last_Name") or "").strip()
        customer = f"{name} {last}".strip() if last else name

        pdt_iso = booking.get("Pickup_Date_Time") or ""
        pt = booking.get("Pickup_Time") or ""
        if not pt and pdt_iso and "T" in pdt_iso:
            pt = pdt_iso.split("T")[1][:5]

        provider_list = booking.get("Provider_List")
        if not provider_list or not isinstance(provider_list, dict):
            return customer, pt, None, pdt_iso

        provider_id = provider_list.get("id")
        if not provider_id:
            return customer, pt, None, pdt_iso

        # Get provider Line_User_ID
        resp2 = requests.get(
            f"{ZOHO_API_BASE}/Providers/{provider_id}",
            headers={"Authorization": f"Zoho-oauthtoken {token}"},
            params={"fields": "Name,Line_User_ID"},
            timeout=10,
        )
        if resp2.status_code != 200:
            return customer, pt, None, pdt_iso

        prov = resp2.json().get("data", [{}])[0]
        line_id = (prov.get("Line_User_ID") or "").strip()
        return customer, pt, (line_id if line_id else None), pdt_iso

    except Exception as e:
        logger.error(f"[DRIVER-TRACK] Zoho lookup error: {e}")
        return None, None, None, None


def _watchdog_check(booking_id):
    """Called 5 minutes after page_opened. If no first ping arrived, alert team."""
    session = tracking_sessions.get(booking_id)
    if not session:
        return
    if session.get("started_at"):
        return  # First ping arrived, normal flow happened
    if session.get("watchdog_fired"):
        return  # Already sent alert

    session["watchdog_fired"] = True
    customer = session.get("customer_name") or "(unknown)"
    pickup_time = session.get("pickup_time") or ""
    line_user_id = session.get("line_user_id")

    msg = (
        f"\u26a0\ufe0f \u0e04\u0e19\u0e02\u0e31\u0e1a\u0e40\u0e1b\u0e34\u0e14\u0e25\u0e34\u0e07\u0e01\u0e4c"
        f"\u0e41\u0e15\u0e48\u0e22\u0e31\u0e07\u0e44\u0e21\u0e48\u0e44\u0e14\u0e49\u0e41\u0e0a\u0e23\u0e4c"
        f"\u0e15\u0e33\u0e41\u0e2b\u0e19\u0e48\u0e07"
        f" \u2014 booking {customer}"
    )
    if pickup_time:
        msg += f" \u0e40\u0e27\u0e25\u0e32 {pickup_time}"

    if line_user_id:
        _line_push(line_user_id, msg)
        logger.info(f"[DRIVER-TRACK] Watchdog alert sent for {booking_id}")
    else:
        logger.warning(f"[DRIVER-TRACK] Watchdog: no line_user_id for {booking_id}, cannot alert")


# ---------------------------------------------------------------------------
# Driver page — captures GPS (driver only)
# ---------------------------------------------------------------------------

@driver_bp.route("/driver/track/<booking_id>", methods=["GET"])
def driver_share_page(booking_id):
    """Serve the driver's location-sharing page. Driver only."""
    _cleanup_stale()

    journey = request.args.get("journey", "transfer")  # "approach" or "transfer"
    now = datetime.now(ICT)

    if booking_id not in tracking_sessions:
        # Look up booking info from Zoho
        customer, pickup_time, line_user_id, pdt_iso = _fetch_booking_and_provider(booking_id)

        tracking_sessions[booking_id] = {
            "lat": None,
            "lng": None,
            "accuracy": None,
            "updated_at": None,
            "started_at": None,
            "page_opened_at": now,
            "customer_name": customer or "",
            "pickup_location": "",
            "pickup_time": pickup_time or "",
            "pickup_datetime_iso": pdt_iso or "",
            "line_user_id": line_user_id,
            "active": True,
            "notified": False,
            "watchdog_fired": False,
        }
        logger.info(
            f"[DRIVER-TRACK] Session created: booking={booking_id}, "
            f"journey={journey}, customer={customer}, line_id={line_user_id and line_user_id[:10]}"
        )

        # Start 5-minute watchdog timer (only for transfer journey)
        if journey == "transfer":
            timer = threading.Timer(300, _watchdog_check, args=[booking_id])
            timer.daemon = True
            timer.start()
            logger.info(f"[DRIVER-TRACK] Watchdog timer started for {booking_id} (5 min)")

    else:
        session = tracking_sessions[booking_id]
        session["active"] = True
        logger.info(f"[DRIVER-TRACK] Session re-opened: booking={booking_id}, journey={journey}")

    session = tracking_sessions[booking_id]
    return render_template(
        "driver_share.html",
        booking_id=booking_id,
        customer_name=session.get("customer_name") or "",
        pickup_time=session.get("pickup_time") or "",
        journey=journey,
        pickup_datetime_iso=session.get("pickup_datetime_iso") or "",
    )


# ---------------------------------------------------------------------------
# Driver GPS pings
# ---------------------------------------------------------------------------

@driver_bp.route("/driver/track/<booking_id>/ping", methods=["POST"])
def driver_ping(booking_id):
    """Receive GPS coordinates from driver's browser."""
    data = request.get_json(silent=True)
    if not data or "lat" not in data or "lng" not in data:
        return jsonify({"status": "error", "message": "Missing lat/lng"}), 400

    journey = data.get("journey", "transfer")  # "approach" or "transfer"
    now = datetime.now(ICT)

    if booking_id not in tracking_sessions:
        tracking_sessions[booking_id] = {
            "lat": None, "lng": None, "accuracy": None, "updated_at": None,
            "started_at": None, "page_opened_at": now,
            "customer_name": "", "pickup_location": "", "pickup_time": "",
            "pickup_datetime_iso": "",
            "line_user_id": None, "active": True, "notified": False,
            "watchdog_fired": False,
        }

    session = tracking_sessions[booking_id]

    # Re-activate session if driver resumes after stop
    if not session.get("active"):
        session["active"] = True
        logger.info(f"[DRIVER-TRACK] Session re-activated via ping: booking={booking_id}")

    session["lat"] = data["lat"]
    session["lng"] = data["lng"]
    session["accuracy"] = data.get("accuracy")
    session["updated_at"] = now.strftime("%Y-%m-%dT%H:%M:%S")

    is_first = session.get("started_at") is None

    if is_first:
        session["started_at"] = now
        logger.info(
            f"[DRIVER-TRACK] First ping: booking={booking_id}, journey={journey}, "
            f"lat={data['lat']:.6f}, lng={data['lng']:.6f}"
        )

        # Look up Zoho if we don't have info yet
        if not session.get("line_user_id") or not session.get("customer_name"):
            customer, pickup_time, line_user_id, pdt_iso = _fetch_booking_and_provider(booking_id)
            if customer:
                session["customer_name"] = customer
            if pickup_time:
                session["pickup_time"] = pickup_time
            if line_user_id:
                session["line_user_id"] = line_user_id
            if pdt_iso:
                session["pickup_datetime_iso"] = pdt_iso

    # Journey-specific Zoho updates
    if journey == "approach":
        # Update Approach GPS fields in Zoho (async, non-blocking)
        t = threading.Thread(
            target=_update_zoho_approach_gps,
            args=(booking_id, data["lat"], data["lng"], is_first),
            daemon=True,
        )
        t.start()
    elif journey == "transfer" and is_first:
        # Transfer journey: send team-view URL on first ping only
        line_user_id = session.get("line_user_id")
        customer = session.get("customer_name") or "(unknown)"
        team_url = f"{BASE_URL}/track/{booking_id}"

        if line_user_id and not session.get("notified"):
            msg = (
                f"\U0001f4cd {customer} \u0e01\u0e33\u0e25\u0e31\u0e07\u0e41\u0e0a\u0e23\u0e4c"
                f"\u0e15\u0e33\u0e41\u0e2b\u0e19\u0e48\u0e07:\n"
                f"{team_url}"
            )
            ok = _line_push(line_user_id, msg)
            if ok:
                session["notified"] = True

    if not is_first:
        logger.debug(
            f"[DRIVER-TRACK] Ping: booking={booking_id}, journey={journey}, "
            f"lat={data['lat']:.6f}, lng={data['lng']:.6f}"
        )

    return jsonify({"status": "ok", "started": is_first, "updated_at": session["updated_at"]}), 200


# ---------------------------------------------------------------------------
# Driver stop
# ---------------------------------------------------------------------------

@driver_bp.route("/driver/track/<booking_id>/stop", methods=["POST"])
def driver_stop(booking_id):
    """Driver stops sharing location."""
    if booking_id in tracking_sessions:
        tracking_sessions[booking_id]["active"] = False
        logger.info(f"[DRIVER-TRACK] Stopped: booking={booking_id}")
    return jsonify({"status": "stopped"}), 200


# ---------------------------------------------------------------------------
# Team viewer — read-only map (team only)
# ---------------------------------------------------------------------------

@driver_bp.route("/track/<booking_id>", methods=["GET"])
def team_view_page(booking_id):
    """Serve the team's read-only map viewer. No GPS capture."""
    session = tracking_sessions.get(booking_id, {})
    return render_template(
        "driver_view.html",
        booking_id=booking_id,
        customer_name=session.get("customer_name") or "",
        pickup_time=session.get("pickup_time") or "",
    )


@driver_bp.route("/track/<booking_id>/data", methods=["GET"])
def team_view_data(booking_id):
    """Return current driver location as JSON (polled by team viewer)."""
    session = tracking_sessions.get(booking_id)
    if not session or session.get("lat") is None:
        return jsonify({
            "status": "waiting",
            "active": session.get("active", False) if session else False,
            "customer_name": session.get("customer_name", "") if session else "",
        }), 200

    return jsonify({
        "status": "ok",
        "lat": session["lat"],
        "lng": session["lng"],
        "accuracy": session.get("accuracy"),
        "updated_at": session.get("updated_at"),
        "active": session.get("active", False),
        "customer_name": session.get("customer_name", ""),
        "pickup_time": session.get("pickup_time", ""),
    }), 200


# ---------------------------------------------------------------------------
# Debug endpoint
# ---------------------------------------------------------------------------

@driver_bp.route("/driver/debug", methods=["GET"])
def driver_debug():
    """Debug endpoint: list active sessions and config."""
    _cleanup_stale()
    sessions_summary = {}
    for bid, s in tracking_sessions.items():
        sessions_summary[bid] = {
            "active": s.get("active"),
            "has_location": s.get("lat") is not None,
            "started_at": str(s.get("started_at") or ""),
            "notified": s.get("notified"),
            "watchdog_fired": s.get("watchdog_fired"),
            "customer_name": s.get("customer_name"),
        }
    return jsonify({
        "token_set": bool(TRANSFER_LINE_TOKEN),
        "base_url": BASE_URL,
        "active_sessions": len([s for s in tracking_sessions.values() if s.get("active")]),
        "sessions": sessions_summary,
    }), 200
