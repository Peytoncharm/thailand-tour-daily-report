"""
approach_watchdog.py
────────────────────
Two cron endpoints that monitor driver approach GPS status
and escalate when drivers don't open their tracking link.

Timeline: T-6hr send link → T-5.5hr soft alert → T-5hr rebroadcast

Blueprint: approach_watchdog_bp
Endpoints:
  /cron/approach-watchdog-soft        — pickup in ~5.5hr, no GPS → soft alert
  /cron/approach-auto-rebroadcast     — pickup in ~5hr, soft-alerted, no GPS → rebroadcast

Env vars:
  DRIVER_OPS_LINE_GROUP_ID  — LINE group for ops alerts
  TRANSFER_LINE_TOKEN       — LINE channel access token (Transfer OA)
"""

import logging
import os
import requests
from datetime import datetime, timezone, timedelta
from flask import Blueprint, jsonify

from zoho_thailand import zoho_coql, zoho_update_record

logger = logging.getLogger(__name__)

approach_watchdog_bp = Blueprint("approach_watchdog", __name__)

ICT = timezone(timedelta(hours=7))
DRIVER_OPS_LINE_GROUP_ID = os.environ.get(
    "DRIVER_OPS_LINE_GROUP_ID", "Cde1194fd7767b00e6393055844ef45bd"
)
TRANSFER_LINE_TOKEN = os.environ.get("TRANSFER_LINE_TOKEN", "")

# DM V1 webhook (workflow 9XvP8N37aj1D5br0)
DM_WEBHOOK_URL = "https://tourtransfer.app.n8n.cloud/webhook/driver-matching"


# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────

def _now_ict() -> datetime:
    """Current time in Asia/Bangkok (ICT, UTC+7)."""
    return datetime.now(ICT)


def _parse_pickup_dt(pdt_str: str):
    """
    Parse Zoho Pickup_Date_Time into a timezone-aware datetime.
    Zoho returns ISO format like '2026-05-04T09:30:00+07:00' or
    sometimes without offset. If no offset, assume ICT.
    """
    if not pdt_str or "T" not in pdt_str:
        return None
    try:
        dt = datetime.fromisoformat(pdt_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ICT)
        return dt
    except (ValueError, TypeError):
        return None


def _push_line_group(message: str) -> bool:
    """
    Push a text message to Driver Ops LINE group.
    Returns True ONLY on HTTP 200. Any failure returns False.
    """
    url = "https://api.line.me/v2/bot/message/push"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {TRANSFER_LINE_TOKEN}",
    }
    body = {
        "to": DRIVER_OPS_LINE_GROUP_ID,
        "messages": [{"type": "text", "text": message}],
    }
    try:
        resp = requests.post(url, json=body, headers=headers, timeout=10)
        if resp.status_code == 200:
            return True
        logger.error(f"[WATCHDOG] LINE push failed: {resp.status_code} {resp.text}")
        return False
    except Exception as e:
        logger.error(f"[WATCHDOG] LINE push exception: {e}")
        return False


def _flag_record(record_id: str, field: str, value: str = "Yes") -> bool:
    """Update a single field on Koh_Chang_Orders. Returns True on success."""
    try:
        zoho_update_record("Koh_Chang_Orders", record_id, {field: value})
        return True
    except Exception as e:
        logger.error(f"[WATCHDOG] Flag update failed {record_id}.{field}: {e}")
        return False


def _query_no_gps_bookings(
    minutes_from_now_start: int,
    minutes_from_now_end: int,
    exclude_flag: str,
) -> list:
    """
    Find bookings where:
      - Approach_Link_Sent = 'Yes'
      - Pickup_Date_Time is between NOW + minutes_from_now_start
        and NOW + minutes_from_now_end (in ICT)
      - Approach_Last_GPS_Time IS NULL (no GPS ping received)
      - exclude_flag field != 'Yes' (idempotency guard)
      - Chanel_of_booking != 'TEST' (filtered in Python, not COQL)
      - Provider_List is populated

    Time math example (soft alert, start=315, end=345):
      NOW = 06:00 ICT
      Window = 11:15 to 11:45 ICT
      A booking with pickup at 11:30 → MATCH (within window)
      A booking with pickup at 12:00 → SKIP (caught next cycle)

    COQL handles the broad filter; Python does precise time-window
    math and != checks (COQL != is unreliable on custom modules).
    """
    now = _now_ict()
    today = now.strftime("%Y-%m-%d")
    tomorrow = (now + timedelta(days=1)).strftime("%Y-%m-%d")

    # Compute window boundaries as absolute ICT datetimes
    window_start = now + timedelta(minutes=minutes_from_now_start)
    window_end = now + timedelta(minutes=minutes_from_now_end)

    # COQL: broad fetch — today/tomorrow, approach sent, no GPS
    # NOTE: No != operators in COQL — all exclusions done in Python
    query = (
        f"select id, Name, Last_Name, Pickup_Date_Time, Transfer_Route, "
        f"Provider_List, Approach_Last_GPS_Time, {exclude_flag}, "
        f"Chanel_of_booking, Approach_Acknowledged, Assignment_Status "
        f"from Koh_Chang_Orders "
        f"where Approach_Link_Sent = 'Yes' "
        f"and Approach_Last_GPS_Time is null "
        f"and (Tour_Date = '{today}' or Tour_Date = '{tomorrow}') "
        f"limit 200"
    )

    try:
        records = zoho_coql(query)
    except Exception as e:
        logger.error(f"[WATCHDOG] COQL failed: {e}")
        return []

    results = []
    for r in records:
        # Python filter: exclude TEST bookings
        if (r.get("Chanel_of_booking") or "").upper() == "TEST":
            continue

        # Idempotency: skip if this flag already set
        if (r.get(exclude_flag) or "") == "Yes":
            continue

        # Skip if no provider assigned
        prov = r.get("Provider_List")
        if not prov or (isinstance(prov, dict) and not prov.get("id")):
            continue

        # Parse pickup and check time window
        pickup_dt = _parse_pickup_dt(r.get("Pickup_Date_Time") or "")
        if pickup_dt is None:
            continue

        # Core time check: is pickup within [window_start, window_end]?
        if pickup_dt < window_start or pickup_dt > window_end:
            continue

        results.append(r)

    return results


# ─────────────────────────────────────────────────────────────
# Endpoint 1: Soft alert — pickup in 5hr15min to 5hr45min, no GPS
# ─────────────────────────────────────────────────────────────
@approach_watchdog_bp.route("/cron/approach-watchdog-soft", methods=["GET", "POST"])
def approach_watchdog_soft():
    """
    Cron: every 5 min.
    Alert: driver got approach link (T-6hr) but hasn't opened GPS.
    Pickup is ~5.5 hours away — early warning to contact provider.
    If no response within 30 min, rebroadcast endpoint takes over.
    """
    bookings = _query_no_gps_bookings(
        minutes_from_now_start=315,   # now + 5hr15min
        minutes_from_now_end=345,     # now + 5hr45min
        exclude_flag="Approach_Soft_Alerted",
    )

    alerted = 0
    for b in bookings:
        pickup_dt = _parse_pickup_dt(b.get("Pickup_Date_Time") or "")
        pickup_time = pickup_dt.strftime("%H:%M") if pickup_dt else "?"
        name = (b.get("Name") or b.get("Last_Name") or "Unknown").strip()
        route = b.get("Transfer_Route") or "No route"
        provider = ""
        prov = b.get("Provider_List")
        if isinstance(prov, dict):
            provider = prov.get("name") or prov.get("Name") or ""

        msg = (
            "\u26a0\ufe0f Driver GPS \u0e44\u0e21\u0e48\u0e40\u0e23\u0e34\u0e48\u0e21"
            " \u2014 pickup \u0e2d\u0e35\u0e01 5.5 \u0e0a\u0e21.\n\n"
            f"\U0001f516 {name}\n"
            f"\u23f0 Pickup: {pickup_time}\n"
            f"\U0001f4cd {route}\n"
            f"\U0001f690 Provider: {provider}\n\n"
            "\u2757 \u0e16\u0e49\u0e32\u0e44\u0e21\u0e48\u0e15\u0e2d\u0e1a\u0e43\u0e19 30 "
            "\u0e19\u0e32\u0e17\u0e35 \u0e23\u0e30\u0e1a\u0e1a\u0e08\u0e30 rebroadcast "
            "\u0e2b\u0e32\u0e04\u0e19\u0e02\u0e31\u0e1a\u0e43\u0e2b\u0e21\u0e48"
            "\u0e2d\u0e31\u0e15\u0e42\u0e19\u0e21\u0e31\u0e15\u0e34\n"
            "\u0e01\u0e23\u0e38\u0e13\u0e32\u0e15\u0e34\u0e14\u0e15\u0e48\u0e2d"
            " provider \u0e17\u0e31\u0e19\u0e17\u0e35"
        )

        # ORDER: push LINE first, flag second.
        # If push fails → flag NOT set → next cron retries.
        if _push_line_group(msg):
            _flag_record(b["id"], "Approach_Soft_Alerted", "Yes")
            alerted += 1

    logger.info(f"[WATCHDOG-SOFT] Alerted {alerted}/{len(bookings)}")
    return jsonify({"status": "ok", "soft_alerted": alerted})


# ─────────────────────────────────────────────────────────────
# Endpoint 2: Auto-rebroadcast — pickup in 4hr45min to 5hr15min
# ─────────────────────────────────────────────────────────────
@approach_watchdog_bp.route("/cron/approach-auto-rebroadcast", methods=["GET", "POST"])
def approach_auto_rebroadcast():
    """
    Cron: every 5 min.
    Last resort: driver completely unresponsive, pickup in ~5hr.
    Only fires on bookings that already went through soft alert.
    Triggers DM V1 to find a replacement driver.

    Loop guard: Assignment_Status must NOT be 'broadcasting'.
    If DM V1 is already active for this booking, skip it.
    No flag write needed — DM V1 itself sets Assignment_Status
    to 'broadcasting', which the loop guard catches next cycle.
    """
    now = _now_ict()
    today = now.strftime("%Y-%m-%d")
    tomorrow = (now + timedelta(days=1)).strftime("%Y-%m-%d")

    # COQL: broad fetch — soft-alerted, approach sent
    # Gate: only rebroadcast bookings that went through soft alert
    # NOTE: No != operators — all exclusions in Python
    query = (
        f"select id, Name, Last_Name, Pickup_Date_Time, Transfer_Route, "
        f"Provider_List, Assignment_Status, Chanel_of_booking, "
        f"Approach_Acknowledged "
        f"from Koh_Chang_Orders "
        f"where Approach_Link_Sent = 'Yes' "
        f"and Approach_Soft_Alerted = 'Yes' "
        f"and (Tour_Date = '{today}' or Tour_Date = '{tomorrow}') "
        f"limit 200"
    )

    try:
        records = zoho_coql(query)
    except Exception as e:
        logger.error(f"[WATCHDOG-REBROADCAST] COQL failed: {e}")
        return jsonify({"status": "error", "detail": str(e)}), 500

    # Time window: pickup in 4hr45min to 5hr15min from now
    window_start = now + timedelta(minutes=285)
    window_end = now + timedelta(minutes=315)

    rebroadcast_count = 0
    skipped_broadcasting = 0

    for r in records:
        # Python filter: exclude TEST bookings
        if (r.get("Chanel_of_booking") or "").upper() == "TEST":
            continue

        # Python filter: skip if acknowledged
        if (r.get("Approach_Acknowledged") or "") == "Yes":
            continue

        # LOOP GUARD: if DM V1 is already broadcasting, skip
        assignment_status = (r.get("Assignment_Status") or "").strip().lower()
        if assignment_status == "broadcasting":
            skipped_broadcasting += 1
            continue

        # Parse pickup time and check window
        pickup_dt = _parse_pickup_dt(r.get("Pickup_Date_Time") or "")
        if pickup_dt is None:
            continue
        if pickup_dt < window_start or pickup_dt > window_end:
            continue

        booking_id = r["id"]
        pickup_time = pickup_dt.strftime("%H:%M")
        name = (r.get("Name") or r.get("Last_Name") or "Unknown").strip()
        route = r.get("Transfer_Route") or "No route"

        # Step 1: Trigger DM V1 rebroadcast via n8n webhook
        try:
            resp = requests.post(
                DM_WEBHOOK_URL,
                json={"id": booking_id},
                timeout=15,
            )
            if resp.status_code not in (200, 201):
                logger.error(
                    f"[WATCHDOG-REBROADCAST] DM webhook non-OK for {booking_id}: "
                    f"{resp.status_code} {resp.text}"
                )
                continue
        except Exception as e:
            logger.error(f"[WATCHDOG-REBROADCAST] DM webhook failed {booking_id}: {e}")
            continue

        # Step 2: Notify group (best-effort)
        msg = (
            "\U0001f504 Auto-Rebroadcast \u0e2a\u0e48\u0e07\u0e41\u0e25\u0e49\u0e27\n\n"
            f"\U0001f516 {name}\n"
            f"\u23f0 Pickup: {pickup_time} (pickup \u0e2d\u0e35\u0e01 5 \u0e0a\u0e21.)\n"
            f"\U0001f4cd {route}\n\n"
            "Provider \u0e40\u0e14\u0e34\u0e21\u0e44\u0e21\u0e48\u0e15\u0e2d\u0e1a GPS"
            " \u2192 broadcast \u0e2b\u0e32\u0e04\u0e19\u0e02\u0e31\u0e1a\u0e43\u0e2b\u0e21\u0e48"
            "\u0e2d\u0e31\u0e15\u0e42\u0e19\u0e21\u0e31\u0e15\u0e34\n"
            "\u0e16\u0e49\u0e32\u0e21\u0e35\u0e04\u0e19\u0e23\u0e31\u0e1a\u0e08\u0e30"
            " assign \u0e17\u0e31\u0e19\u0e17\u0e35"
        )
        _push_line_group(msg)

        # No flag write needed here.
        # DM V1 sets Assignment_Status = 'broadcasting' which
        # prevents re-trigger on next cron cycle via loop guard above.
        rebroadcast_count += 1

    logger.info(
        f"[WATCHDOG-REBROADCAST] Rebroadcast {rebroadcast_count}, "
        f"skipped {skipped_broadcasting} already-broadcasting"
    )
    return jsonify({
        "status": "ok",
        "rebroadcast": rebroadcast_count,
        "skipped_broadcasting": skipped_broadcasting,
    })
