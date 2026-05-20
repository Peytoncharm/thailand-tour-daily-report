"""
DEPRECATED as of 2026-05-03.
Pre-pickup reminders are now handled by n8n workflow 2LdDPFybPPSGKvfp
("Pre-Pickup Reminder Transfer (30 min)"). The /cron/pre-pickup-reminder
endpoint in app.py is a no-op. This module is kept for reference only.
"""

import os
import logging
import uuid as _uuid
import requests
from datetime import datetime, timezone, timedelta

from zoho_thailand import zoho_get_records

logger = logging.getLogger(__name__)

ICT = timezone(timedelta(hours=7))

TRANSFER_LINE_TOKEN = os.environ.get("TRANSFER_LINE_TOKEN", "")
TEAM_NOTIFY_GROUP_ID = "C9ff8de09378cba9f1a8a53a04b707a0a"
BASE_URL = "https://thailand-tour-daily-report.onrender.com"

TRANSFER_TYPES = {"Private Transfer"}

ORDER_FIELDS = (
    "Name,Last_Name,Tour_Date,Type_of_Package,Pickup_Date_Time,Pickup_Time,"
    "Pickup_Location,Pickup_location,Dropoff_Location,Provider_List,"
    "Number_of_People,Chanel_of_booking"
)

PROVIDER_FIELDS = "Name,Line_User_ID,Line_User_Id"

# Dedup: {zoho_order_id: timestamp_sent}
_reminded = {}


def _cleanup_old(max_age_hours=24):
    """Remove entries older than max_age_hours to prevent memory leak."""
    cutoff = datetime.now(ICT) - timedelta(hours=max_age_hours)
    stale = [k for k, v in _reminded.items() if v < cutoff]
    for k in stale:
        del _reminded[k]


def _parse_pickup_datetime(order):
    """Parse Pickup_Date_Time into a datetime in ICT, or None."""
    pdt = order.get("Pickup_Date_Time") or ""
    if not pdt:
        return None
    try:
        if "T" in pdt:
            # Format: 2026-05-02T09:00:00+07:00 or 2026-05-02T09:00:00
            raw = pdt.split("+")[0].split("Z")[0]
            dt = datetime.strptime(raw, "%Y-%m-%dT%H:%M:%S")
        else:
            dt = datetime.strptime(pdt, "%Y-%m-%d %H:%M:%S")
        return dt.replace(tzinfo=ICT)
    except (ValueError, TypeError):
        return None


def _get_pickup_time_str(order):
    """Get a display-friendly pickup time string."""
    pt = order.get("Pickup_Time") or ""
    if pt:
        return pt
    pdt = _parse_pickup_datetime(order)
    if pdt:
        return pdt.strftime("%H:%M")
    return ""


def _get_pickup_location(order):
    return (order.get("Pickup_Location") or order.get("Pickup_location") or "").strip()


def _get_customer_name(order):
    name = (order.get("Name") or "").strip()
    last = (order.get("Last_Name") or "").strip()
    if last:
        return f"{name} {last}"
    return name


def _fetch_provider_line_id(provider_id):
    """Fetch a single provider's LINE User ID from Zoho."""
    if not provider_id:
        return None, None
    try:
        from zoho_thailand import _get_access_token, ZOHO_API_BASE
        token = _get_access_token()
        if not token:
            return None, None
        resp = requests.get(
            f"{ZOHO_API_BASE}/Providers/{provider_id}",
            headers={"Authorization": f"Zoho-oauthtoken {token}"},
            params={"fields": PROVIDER_FIELDS},
            timeout=10,
        )
        if resp.status_code != 200:
            logger.warning(f"[PRE-PICKUP] Provider {provider_id} fetch failed: {resp.status_code}")
            return None, None
        data = resp.json().get("data", [{}])[0]
        line_id = data.get("Line_User_ID") or data.get("Line_User_Id") or ""
        prov_name = data.get("Name") or ""
        return line_id.strip() if line_id else None, prov_name
    except Exception as e:
        logger.error(f"[PRE-PICKUP] Provider fetch error: {e}")
        return None, None


def _line_push(to, text):
    """Send a LINE push message. Returns True on success."""
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
            timeout=5,
        )
        return resp.status_code == 200
    except Exception as e:
        logger.error(f"[PRE-PICKUP] LINE push error: {e}")
        return False


def run_pre_pickup(dry_run=False):
    """Main entry point. If dry_run=True, skip LINE sends."""
    now = datetime.now(ICT)
    today_str = now.strftime("%Y-%m-%d")
    window_end = now + timedelta(minutes=30)

    logger.info(f"[PRE-PICKUP] Running for {now.strftime('%H:%M')} ICT, window until {window_end.strftime('%H:%M')}")

    _cleanup_old()

    # Fetch today's orders (all, then filter in-memory)
    records = zoho_get_records("Koh_Chang_Orders", fields=ORDER_FIELDS)
    logger.info(f"[PRE-PICKUP] Fetched {len(records)} total orders")

    # Filter to today's transfers with Pickup_Date_Time in window
    candidates = []
    for r in records:
        # Must be transfer type
        pkg = (r.get("Type_of_Package") or "").strip()
        if pkg not in TRANSFER_TYPES:
            continue
        # Exclude TEST
        if (r.get("Chanel_of_booking") or "").upper() == "TEST":
            continue
        # Must have provider
        pl = r.get("Provider_List")
        if not pl or not isinstance(pl, dict) or not pl.get("id"):
            continue
        # Check Tour_Date is today
        tour_date = (r.get("Tour_Date") or "").split("T")[0]
        if tour_date != today_str:
            continue
        # Check Pickup_Date_Time in window
        pdt = _parse_pickup_datetime(r)
        if not pdt:
            continue
        if not (now <= pdt <= window_end):
            continue
        # Skip if already reminded
        order_id = r.get("id", "")
        if order_id in _reminded:
            continue

        candidates.append(r)

    logger.info(f"[PRE-PICKUP] {len(candidates)} bookings in 30-min window, not yet reminded")

    stats = {"checked": len(records), "in_window": len(candidates), "sent": 0, "skipped_no_line": 0}

    for order in candidates:
        order_id = order.get("id", "")
        pl = order["Provider_List"]
        prov_id = pl.get("id", "")
        prov_display = pl.get("name", "")

        # Fetch provider LINE ID
        line_id, prov_name = _fetch_provider_line_id(prov_id)
        if not line_id:
            logger.warning(f"[PRE-PICKUP] No LINE ID for provider {prov_display} ({prov_id}), skipping")
            stats["skipped_no_line"] += 1
            continue

        # Build tracking link
        tracking_uuid = str(_uuid.uuid4())[:12]
        customer = _get_customer_name(order)
        pickup = _get_pickup_location(order)
        time_str = _get_pickup_time_str(order)

        tracking_url = (
            f"{BASE_URL}/driver/track/{tracking_uuid}"
            f"?name={requests.utils.quote(customer)}"
            f"&pickup={requests.utils.quote(pickup)}"
            f"&time={requests.utils.quote(time_str)}"
        )

        # Pre-create tracking session so driver page works immediately
        from driver_location import tracking_sessions
        tracking_sessions[tracking_uuid] = {
            "lat": None, "lng": None, "accuracy": None, "updated_at": None,
            "name": customer, "pickup": pickup, "time": time_str,
            "active": True, "notified": False,
        }

        # Send reminder to driver
        driver_msg = (
            "📍 อีก 30 นาทีถึงเวลารับลูกค้า\n"
            f"👤 {customer}\n"
            f"📍 จาก: {pickup}\n"
            f"⏰ เวลา: {time_str}\n"
            "\n"
            "กดลิงก์นี้เพื่อเริ่มแชร์ตำแหน่ง:\n"
            f"{tracking_url}"
        )

        if dry_run:
            _reminded[order_id] = now
            stats["sent"] += 1
            logger.info(f"[PRE-PICKUP] DRY RUN: would send to {prov_display} for {customer}")
        else:
            ok = _line_push(line_id, driver_msg)
            if ok:
                _reminded[order_id] = now
                stats["sent"] += 1
                logger.info(f"[PRE-PICKUP] Sent to {prov_display} for {customer} (uuid={tracking_uuid})")

                # Also send team the viewer link
                view_url = f"{BASE_URL}/driver/track/{tracking_uuid}/view"
                team_msg = (
                    "🚐 Pre-pickup reminder ส่งแล้ว\n"
                    f"👤 ลูกค้า: {customer}\n"
                    f"📍 จาก: {pickup}\n"
                    f"⏰ เวลา: {time_str}\n"
                    f"🚗 คนขับ: {prov_display}\n"
                    f"🗺️ ดูตำแหน่ง: {view_url}"
                )
                _line_push(TEAM_NOTIFY_GROUP_ID, team_msg)
            else:
                logger.error(f"[PRE-PICKUP] Failed to send to {prov_display} ({line_id})")

    logger.info(f"[PRE-PICKUP] Done: {stats}")
    return stats
