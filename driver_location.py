import os
import logging
import requests
from datetime import datetime, timezone, timedelta
from flask import Blueprint, request, jsonify, render_template

logger = logging.getLogger(__name__)

driver_bp = Blueprint("driver", __name__)

ICT = timezone(timedelta(hours=7))

# In-memory tracking sessions: {uuid: {lat, lng, accuracy, updated_at, name, pickup, time, active}}
tracking_sessions = {}

# LINE debug config (kept for /driver/debug endpoint)
TRANSFER_LINE_TOKEN = os.environ.get("TRANSFER_LINE_TOKEN", "")
TRANSFER_LINE_GROUP_ID = "C03b8de018aa2076157d032bc9b0ae279"


# ---------------------------------------------------------------------------
# Debug endpoint (kept from previous version)
# ---------------------------------------------------------------------------

@driver_bp.route("/driver/debug", methods=["GET"])
def driver_debug():
    """Debug endpoint: verify LINE token identity and group push ability."""
    info = {}
    token_prefix = TRANSFER_LINE_TOKEN[:8] if TRANSFER_LINE_TOKEN else "(empty)"
    info["token_prefix"] = token_prefix
    info["group_id"] = TRANSFER_LINE_GROUP_ID
    info["active_sessions"] = len([s for s in tracking_sessions.values() if s.get("active")])

    try:
        bot_res = requests.get(
            "https://api.line.me/v2/bot/info",
            headers={"Authorization": f"Bearer {TRANSFER_LINE_TOKEN}"},
            timeout=10
        )
        info["bot_info_status"] = bot_res.status_code
        info["bot_info"] = bot_res.json() if bot_res.status_code == 200 else bot_res.text
    except Exception as e:
        info["bot_info_error"] = str(e)

    try:
        group_res = requests.get(
            f"https://api.line.me/v2/bot/group/{TRANSFER_LINE_GROUP_ID}/summary",
            headers={"Authorization": f"Bearer {TRANSFER_LINE_TOKEN}"},
            timeout=10
        )
        info["group_summary_status"] = group_res.status_code
        info["group_summary"] = group_res.json() if group_res.status_code == 200 else group_res.text
    except Exception as e:
        info["group_summary_error"] = str(e)

    return jsonify(info), 200


# ---------------------------------------------------------------------------
# Driver tracking — share page (driver opens this)
# ---------------------------------------------------------------------------

@driver_bp.route("/driver/track/<uuid>", methods=["GET"])
def driver_share_page(uuid):
    """Serve the driver's location-sharing page."""
    name = request.args.get("name", "")
    pickup = request.args.get("pickup", "")
    time_str = request.args.get("time", "")

    # Create session on first visit if it doesn't exist
    if uuid not in tracking_sessions:
        tracking_sessions[uuid] = {
            "lat": None,
            "lng": None,
            "accuracy": None,
            "updated_at": None,
            "name": name,
            "pickup": pickup,
            "time": time_str,
            "active": True,
        }
        logger.info(f"[DRIVER-TRACK] Session created: uuid={uuid}, name={name}, pickup={pickup}")
    else:
        # Update booking info if provided (in case of re-open with params)
        if name:
            tracking_sessions[uuid]["name"] = name
        if pickup:
            tracking_sessions[uuid]["pickup"] = pickup
        if time_str:
            tracking_sessions[uuid]["time"] = time_str
        # Re-activate if driver re-opens
        tracking_sessions[uuid]["active"] = True
        logger.info(f"[DRIVER-TRACK] Session re-opened: uuid={uuid}")

    return render_template("driver_share.html", uuid=uuid, name=name, pickup=pickup, time=time_str)


# ---------------------------------------------------------------------------
# Driver tracking — GPS updates from driver's browser
# ---------------------------------------------------------------------------

@driver_bp.route("/driver/track/<uuid>/update", methods=["POST"])
def driver_update(uuid):
    """Receive GPS coordinates from driver's browser."""
    data = request.get_json(silent=True)
    if not data or "lat" not in data or "lng" not in data:
        return jsonify({"status": "error", "message": "Missing lat/lng"}), 400

    if uuid not in tracking_sessions:
        tracking_sessions[uuid] = {
            "name": "", "pickup": "", "time": "", "active": True
        }

    session = tracking_sessions[uuid]
    if not session.get("active"):
        return jsonify({"status": "stopped", "message": "Session stopped"}), 200

    now = datetime.now(ICT).strftime("%Y-%m-%dT%H:%M:%S")
    session["lat"] = data["lat"]
    session["lng"] = data["lng"]
    session["accuracy"] = data.get("accuracy")
    session["updated_at"] = now

    logger.info(
        f"[DRIVER-TRACK] Update: uuid={uuid}, "
        f"lat={data['lat']:.6f}, lng={data['lng']:.6f}, "
        f"accuracy={data.get('accuracy', '?')}m"
    )

    return jsonify({"status": "ok", "updated_at": now}), 200


# ---------------------------------------------------------------------------
# Driver tracking — stop sharing
# ---------------------------------------------------------------------------

@driver_bp.route("/driver/track/<uuid>/stop", methods=["POST"])
def driver_stop(uuid):
    """Driver stops sharing location."""
    if uuid in tracking_sessions:
        tracking_sessions[uuid]["active"] = False
        logger.info(f"[DRIVER-TRACK] Stopped: uuid={uuid}")
    return jsonify({"status": "stopped"}), 200


# ---------------------------------------------------------------------------
# Team viewer — watch driver's location
# ---------------------------------------------------------------------------

@driver_bp.route("/driver/track/<uuid>/view", methods=["GET"])
def team_view_page(uuid):
    """Serve the team's viewer page showing driver location on map."""
    session = tracking_sessions.get(uuid, {})
    name = session.get("name", "")
    pickup = session.get("pickup", "")
    time_str = session.get("time", "")
    return render_template("driver_view.html", uuid=uuid, name=name, pickup=pickup, time=time_str)


# ---------------------------------------------------------------------------
# Team viewer — JSON status endpoint (polled by viewer page)
# ---------------------------------------------------------------------------

@driver_bp.route("/driver/track/<uuid>/status", methods=["GET"])
def driver_status(uuid):
    """Return current driver location as JSON (polled by viewer page)."""
    session = tracking_sessions.get(uuid)
    if not session or session.get("lat") is None:
        return jsonify({
            "status": "waiting",
            "message": "Driver has not shared location yet",
            "active": session.get("active", False) if session else False
        }), 200

    return jsonify({
        "status": "ok",
        "lat": session["lat"],
        "lng": session["lng"],
        "accuracy": session.get("accuracy"),
        "updated_at": session.get("updated_at"),
        "active": session.get("active", False),
        "name": session.get("name", ""),
        "pickup": session.get("pickup", ""),
        "time": session.get("time", ""),
    }), 200
