import os
import logging
import requests
from flask import Blueprint, request, jsonify, render_template

logger = logging.getLogger(__name__)

driver_bp = Blueprint("driver", __name__)

TRANSFER_LINE_TOKEN = os.environ.get("TRANSFER_LINE_TOKEN", "")
TRANSFER_LINE_GROUP_ID = "C03b8de018aa2076157d032bc9b0ae279"


def _push_line_location(message):
    """Push a text message to the Transfer LINE group."""
    url = "https://api.line.me/v2/bot/message/push"
    headers = {
        "Authorization": f"Bearer {TRANSFER_LINE_TOKEN}",
        "Content-Type": "application/json"
    }
    payload = {
        "to": TRANSFER_LINE_GROUP_ID,
        "messages": [{"type": "text", "text": message}]
    }
    token_prefix = TRANSFER_LINE_TOKEN[:8] if TRANSFER_LINE_TOKEN else "(empty)"
    logger.info(f"[DRIVER-LOC] Pushing to group={TRANSFER_LINE_GROUP_ID}, token_prefix={token_prefix}...")
    try:
        res = requests.post(url, headers=headers, json=payload, timeout=15)
        if res.status_code != 200:
            logger.error(
                f"[DRIVER-LOC] LINE push error {res.status_code}: {res.text} "
                f"| group={TRANSFER_LINE_GROUP_ID} | token_prefix={token_prefix}"
            )
        else:
            logger.info("[DRIVER-LOC] LINE push OK")
        return res.status_code
    except Exception as e:
        logger.error(f"[DRIVER-LOC] LINE push exception: {e}")
        return 500


@driver_bp.route("/driver/debug", methods=["GET"])
def driver_debug():
    """Debug endpoint: verify LINE token identity and group push ability."""
    import json as _json
    info = {}

    # Check which bot this token belongs to
    token_prefix = TRANSFER_LINE_TOKEN[:8] if TRANSFER_LINE_TOKEN else "(empty)"
    info["token_prefix"] = token_prefix
    info["group_id"] = TRANSFER_LINE_GROUP_ID

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

    # Check group membership
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


@driver_bp.route("/driver/loc/<token>", methods=["GET"])
def driver_page(token):
    """Serve the location-sharing page."""
    return render_template("driver_location.html", token=token)


@driver_bp.route("/driver/loc/<token>", methods=["POST"])
def driver_submit(token):
    """Receive GPS coordinates and push to LINE."""
    data = request.get_json(silent=True)
    if not data or "lat" not in data or "lng" not in data:
        return jsonify({"status": "error", "message": "Missing lat/lng"}), 400

    lat = data["lat"]
    lng = data["lng"]
    accuracy = data.get("accuracy", "")
    name = data.get("name", "")
    pickup = data.get("pickup", "")
    time_str = data.get("time", "")

    maps_url = f"https://maps.google.com/maps?q={lat},{lng}"

    # Build LINE message
    lines = ["\U0001f4cd \u0e15\u0e33\u0e41\u0e2b\u0e19\u0e48\u0e07\u0e04\u0e19\u0e02\u0e31\u0e1a"]
    if name:
        lines.append(f"\U0001f464 \u0e25\u0e39\u0e01\u0e04\u0e49\u0e32: {name}")
    if pickup:
        lines.append(f"\U0001f4cd Pickup: {pickup}")
    if time_str:
        lines.append(f"\u23f0 Time: {time_str}")
    if accuracy:
        lines.append(f"\U0001f3af Accuracy: {int(float(accuracy))}m")
    lines.append(maps_url)

    message = "\n".join(lines)
    status_code = _push_line_location(message)

    if status_code == 200:
        logger.info(f"[DRIVER-LOC] Token={token}, lat={lat}, lng={lng}, name={name}")
        return jsonify({"status": "ok", "maps_url": maps_url}), 200
    else:
        return jsonify({"status": "error", "message": "LINE push failed"}), 502
