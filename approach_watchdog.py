"""
approach_watchdog.py
────────────────────
Cron endpoints for the driver approach GPS timeline.

Timeline: T-6hr send link → T-5.5hr soft alert → T-5hr rebroadcast

Blueprint: approach_watchdog_bp
Endpoints:
  /cron/approach-send                 — pickup in ~6hr → LINE driver the GPS link
  /cron/approach-watchdog-soft        — pickup in ~5.5hr, no GPS → soft alert
  /cron/approach-auto-rebroadcast     — pickup in ~5hr, soft-alerted, no GPS → PROPOSE rebroadcast (team decides)
  /rebroadcast/decision               — team tap relay: [ใช่] → DM webhook with manual_approved

Env vars:
  DRIVER_OPS_LINE_GROUP_ID  — LINE group for ops alerts
  TRANSFER_LINE_TOKEN       — LINE channel access token (Transfer OA)
"""

import logging
import os
import requests
from datetime import datetime, timezone, timedelta
from flask import Blueprint, jsonify, request

from zoho_thailand import zoho_search, zoho_update_record
from provider_guard import should_block, alert_pa_blocked

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


def _push_group_flex(alt_text: str, bubble: dict) -> bool:
    """Push a flex bubble to the Driver Ops group. True only on HTTP 200."""
    url = "https://api.line.me/v2/bot/message/push"
    headers = {
        "Content-Type": "application/json; charset=utf-8",
        "Authorization": f"Bearer {TRANSFER_LINE_TOKEN}",
    }
    body = {
        "to": DRIVER_OPS_LINE_GROUP_ID,
        "messages": [{"type": "flex", "altText": alt_text, "contents": bubble}],
    }
    try:
        resp = requests.post(url, json=body, headers=headers, timeout=10)
        if resp.status_code == 200:
            return True
        logger.error(f"[WATCHDOG] LINE flex push failed: {resp.status_code} {resp.text}")
        return False
    except Exception as e:
        logger.error(f"[WATCHDOG] LINE flex push exception: {e}")
        return False


def _flag_record(record_id: str, field: str, value: str = "Yes") -> bool:
    """Update a single field on Koh_Chang_Orders. Returns True on success.
    Stage 1.2: after a successful Zoho write, immediately patch the same
    field in the local booking cache so sibling crons inside the same
    15-min sweep window see it (race fix — no waiting for the next sweep)."""
    try:
        zoho_update_record("Koh_Chang_Orders", record_id, {field: value})
        try:
            from booking_cache import update_cached_field
            update_cached_field(record_id, field, value)
        except Exception:
            pass  # cache patch is best-effort; Zoho write already succeeded
        return True
    except Exception as e:
        logger.error(f"[WATCHDOG] Flag update failed {record_id}.{field}: {e}")
        return False


def _cache_window(days, package=None):
    """Cache-first day-window read. Returns a list of raw Zoho-shaped
    records, or None when the cache is unavailable/unprimed — callers
    then fall back to their original direct Zoho searches."""
    try:
        from booking_cache import get_bookings_for_dates
        recs = get_bookings_for_dates(days, type_of_package=package)
        return recs if recs else None
    except Exception as e:
        logger.warning(f"[WATCHDOG] cache window failed (Zoho fallback): {e}")
        return None


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
      - Chanel_of_booking != 'TEST' (filtered in Python)
      - Provider_List is populated
      - Type_of_Package = 'Private Transfer'

    Uses REST /search API (not COQL — token lacks COQL scope).
    REST criteria can't check "is null", so we fetch all
    Approach_Link_Sent=Yes bookings for today/tomorrow and
    filter for null GPS + time window in Python.
    """
    now = _now_ict()
    today = now.strftime("%Y-%m-%d")
    tomorrow = (now + timedelta(days=1)).strftime("%Y-%m-%d")

    window_start = now + timedelta(minutes=minutes_from_now_start)
    window_end = now + timedelta(minutes=minutes_from_now_end)

    fields = (
        "id,Name,Last_Name,Pickup_Date_Time,Transfer_Route,"
        "Provider_List,Approach_Last_GPS_Time," + exclude_flag + ","
        "Chanel_of_booking,Approach_Acknowledged,Assignment_Status,"
        "Type_of_Package"
    )

    # Cache-first (Stage 1.2): all of today+tomorrow from booking_cache,
    # then the Approach_Link_Sent=Yes criteria as a Python filter.
    # Cache unavailable/unprimed -> original direct REST searches.
    cached = _cache_window([today, tomorrow])
    if cached is not None:
        all_records = [r for r in cached
                       if (r.get("Approach_Link_Sent") or "") == "Yes"]
        logger.info(f"[WATCHDOG] cache returned {len(all_records)} flagged records")
    else:
        all_records = []
        criteria_today = (
            "(Approach_Link_Sent:equals:Yes)"
            f"and(Tour_Date:equals:{today})"
        )
        all_records.extend(zoho_search("Koh_Chang_Orders", criteria_today, fields))
        criteria_tomorrow = (
            "(Approach_Link_Sent:equals:Yes)"
            f"and(Tour_Date:equals:{tomorrow})"
        )
        all_records.extend(zoho_search("Koh_Chang_Orders", criteria_tomorrow, fields))
        logger.info(f"[WATCHDOG] REST search returned {len(all_records)} records")

    results = []
    for r in all_records:
        # Must have no GPS ping (REST can't filter "is null")
        if r.get("Approach_Last_GPS_Time"):
            continue

        # Private Transfer only
        if (r.get("Type_of_Package") or "") != "Private Transfer":
            continue

        # Exclude TEST bookings
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

        if pickup_dt < window_start or pickup_dt > window_end:
            continue

        results.append(r)

    return results


# ─────────────────────────────────────────────────────────────
# Endpoint 0: T-6hr send — LINE the driver the GPS tracking link
# Migrated from n8n workflow QhGJYtjYjvRq1OD0
# ("Driver Approach Tracking — Send Link") on 7 Aug 2026 after
# n8n plan execution limits silently stopped sends from 21 May.
# ─────────────────────────────────────────────────────────────

TRACK_URL_BASE = "https://thailand-tour-daily-report.onrender.com/driver/track/"

# In-memory guard so the empty-UID heads-up isn't re-posted to the
# ops group every 15 min for the same booking. Resets on redeploy —
# worst case is one repeat alert, which is acceptable.
_no_uid_alerted = set()


def _fetch_provider(provider_id: str):
    """Fetch provider record from Zoho. Returns dict or None on error."""
    from zoho_thailand import _get_access_token, ZOHO_API_BASE
    token = _get_access_token()
    if not token:
        logger.error("[APPROACH-SEND] No Zoho token for provider fetch")
        return None
    try:
        resp = requests.get(
            f"{ZOHO_API_BASE}/Providers/{provider_id}",
            headers={"Authorization": f"Zoho-oauthtoken {token}"},
            params={"fields": "id,Name,Line_User_ID,Phone_1,Outsourced_Agent"},
            timeout=10,
        )
        if resp.status_code != 200:
            logger.error(
                f"[APPROACH-SEND] Provider fetch {provider_id} failed: "
                f"{resp.status_code} {resp.text}"
            )
            return None
        data = resp.json().get("data") or []
        return data[0] if data else None
    except Exception as e:
        logger.error(f"[APPROACH-SEND] Provider fetch exception {provider_id}: {e}")
        return None


def _push_line_uid(uid: str, text: str) -> bool:
    """Push a text message to a single LINE UID. True only on HTTP 200."""
    if not TRANSFER_LINE_TOKEN or not uid:
        return False
    try:
        resp = requests.post(
            "https://api.line.me/v2/bot/message/push",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {TRANSFER_LINE_TOKEN}",
            },
            json={"to": uid, "messages": [{"type": "text", "text": text}]},
            timeout=10,
        )
        if resp.status_code == 200:
            return True
        logger.error(
            f"[APPROACH-SEND] LINE push to {uid} failed: "
            f"{resp.status_code} {resp.text}"
        )
        return False
    except Exception as e:
        logger.error(f"[APPROACH-SEND] LINE push exception to {uid}: {e}")
        return False


def _build_approach_message(booking: dict) -> str:
    """
    Thai driver message + GPS tracking link.
    Copied VERBATIM from n8n workflow QhGJYtjYjvRq1OD0
    node "Build Approach Message" — do not reword without
    updating the Driver Ops SOP.
    """
    booking_id = booking.get("id") or ""
    full_name = (booking.get("Name") or "").strip()
    tour_date = (booking.get("Tour_Date") or "").split("T")[0]
    route = booking.get("Transfer_Route") or ""
    pickup = booking.get("Pickup_Location") or ""

    pickup_time = ""
    pdt = booking.get("Pickup_Date_Time") or ""
    if pdt and "T" in pdt:
        pickup_time = pdt.split("T")[1][:5]

    track_url = f"{TRACK_URL_BASE}{booking_id}?journey=approach"

    return (
        "\U0001f690 งาน Transfer — เปิด GPS "
        "เพื่อยืนยันว่า"
        "จะมารับลูกค้า\n\n"
        f"ลูกค้า: {full_name}\n"
        f"\U0001f4c5 {tour_date} ⏰ {pickup_time}\n"
        f"\U0001f4cd {route}\n"
        f"   จาก: {pickup}\n\n"
        "⚠️ กรุณาเปิดลิง"
        "ก์นี้และเริ่มแ"
        "ชร์ตำแหน่งเพื่"
        "อให้ทีมเห็นว่า"
        "ท่านพร้อมมารับ"
        "ลูกค้า\n\n"
        f"{track_url}\n\n"
        "ทีมจะเห็นตำแห"
        "น่งและเส้นทาง"
        "ของท่าน real-time\n"
        "ถ้าไม่เปิด GPS ภาย"
        "ใน 30 นาที ทีมจะติ"
        "ดต่อตรวจสอบ\n\n"
        "ถ้าไม่สามารถมา"
        "รับงานได้ กรุณา"
        "แจ้งทีมทันที"
    )


def _build_customer_email(booking: dict, provider: dict, track_url: str):
    """(subject, html) for the pre-pickup customer tracking email.
    English, short, plain HTML, mobile-friendly."""
    customer_first = ((booking.get("Name") or "").strip().split(" ") or [""])[0]
    pickup_time = ""
    pdt = booking.get("Pickup_Date_Time") or ""
    if pdt and "T" in pdt:
        pickup_time = pdt.split("T")[1][:5]

    driver_first = ((provider.get("Name") or "").strip().split(" ") or [""])[0]
    car_model = (provider.get("Car_Model") or "").strip()
    car_colour = (provider.get("Car_Colour") or "").strip()
    car_reg = (provider.get("Vehicle_Registration") or "").strip()
    phone = (provider.get("Phone_1") or "").strip()

    vehicle = " ".join(x for x in [car_colour, car_model] if x)

    subject = f"Your driver is on the way — pickup {pickup_time}".strip()

    rows = []
    if driver_first:
        rows.append(f"<b>Driver:</b> {driver_first}")
    if vehicle:
        rows.append(f"<b>Vehicle:</b> {vehicle}")
    if car_reg:
        rows.append(f"<b>Registration:</b> {car_reg}")
    if phone:
        rows.append(f'<b>Driver phone:</b> <a href="tel:{phone}">{phone}</a>')

    html = (
        '<div style="font-family:-apple-system,Segoe UI,Arial,sans-serif;'
        'font-size:15px;color:#333;line-height:1.7;max-width:480px">'
        f"<p>Hi {customer_first or 'there'},</p>"
        f"<p>Your driver will pick you up at <b>{pickup_time}</b>.</p>"
        f"<p>{'<br>'.join(rows)}</p>"
        f'<p><a href="{track_url}" style="display:inline-block;'
        'background:#1565c0;color:#fff;padding:10px 18px;border-radius:8px;'
        'text-decoration:none">Watch your driver approach live</a></p>'
        "<p>See you soon!<br>Peyton &amp; Charmed Transfers</p>"
        "</div>"
    )
    return subject, html


def _customer_email_pass(dry_run: bool) -> dict:
    """
    Customer tracking email at ~45 min before pickup.
    Runs inside the same 15-min cron pass as the T-6 link send.

    Window: pickup between now+30m and now+75m ICT.
    Idempotent via Customer_Track_Link_Sent (same pattern as
    Approach_Link_Sent). Deliberately a SEPARATE Zoho query so a
    missing Customer_Track_Link_Sent field (not yet created) can
    never break the driver link sends — the search just errors,
    returns [], and this pass no-ops.
    """
    now = _now_ict()
    window_start = now + timedelta(minutes=30)
    window_end = now + timedelta(minutes=75)

    fields = (
        "id,Name,Last_Name,Tour_Date,Pickup_Date_Time,Type_of_Package,"
        "Transfer_Route,Pickup_Location,Provider_List,Chanel_of_booking,"
        "Status,Email,Customer_Track_Link_Sent"
    )

    _days = [now.strftime("%Y-%m-%d"),
             (now + timedelta(days=1)).strftime("%Y-%m-%d")]
    # Cache-first (Stage 1.2); fallback = original direct searches
    records = _cache_window(_days, package="Private Transfer")
    if records is None:
        records = []
        for day in _days:
            criteria = (
                "(Type_of_Package:equals:Private Transfer)"
                f"and(Tour_Date:equals:{day})"
            )
            records.extend(zoho_search("Koh_Chang_Orders", criteria, fields))

    stats = {"emailed": 0, "skipped_no_email": 0, "skipped_outsourced": 0,
             "errors": 0, "candidates": []}
    seen = set()

    for b in records:
        bid = b.get("id")
        if not bid or bid in seen:
            continue
        seen.add(bid)

        if (b.get("Status") or "").strip() != "Confirmed":
            continue
        if (b.get("Chanel_of_booking") or "").strip().upper() == "TEST":
            continue
        if (b.get("Customer_Track_Link_Sent") or "") == "Yes":
            continue

        prov_ref = b.get("Provider_List")
        prov_id = prov_ref.get("id") if isinstance(prov_ref, dict) else None
        if not prov_id:
            continue

        pickup_dt = _parse_pickup_dt(b.get("Pickup_Date_Time") or "")
        if pickup_dt is None or pickup_dt < window_start or pickup_dt > window_end:
            continue

        email = (b.get("Email") or "").strip()
        if not email or "@" not in email:
            stats["skipped_no_email"] += 1
            continue

        blocked, block_reason = should_block(
            provider_id=prov_id,
            booking={"Type_of_Package": b.get("Type_of_Package")},
        )
        if blocked:
            logger.warning(f"[CUST-EMAIL] Blocked for {bid}: {block_reason}")
            continue

        provider = _fetch_provider(prov_id)
        if provider is None:
            stats["errors"] += 1
            continue
        if provider.get("Outsourced_Agent") is True:
            stats["skipped_outsourced"] += 1
            continue

        from customer_track import customer_link
        track_url = customer_link(bid)
        subject, html = _build_customer_email(b, provider, track_url)

        if dry_run:
            stats["candidates"].append({
                "booking_id": bid,
                "to": email,
                "subject": subject,
                "track_url": track_url,
            })
            continue

        # Send first, flag second — failed send retries next run.
        from email_sender import send_email
        if not send_email(email, subject, html):
            logger.error(f"[CUST-EMAIL] Send FAILED for {bid} to {email}")
            stats["errors"] += 1
            continue

        if _flag_record(bid, "Customer_Track_Link_Sent", "Yes"):
            stats["emailed"] += 1
        else:
            logger.error(
                f"[CUST-EMAIL] CRITICAL: email sent for {bid} but "
                "Customer_Track_Link_Sent write-back FAILED — set it in "
                "Zoho manually to avoid a duplicate email"
            )
            stats["errors"] += 1

    logger.info(
        f"[CUST-EMAIL] dry_run={dry_run} emailed={stats['emailed']} "
        f"candidates={len(stats['candidates'])} no_email={stats['skipped_no_email']} "
        f"outsourced={stats['skipped_outsourced']} errors={stats['errors']}"
    )
    return stats


@approach_watchdog_bp.route("/cron/approach-send", methods=["GET", "POST"])
def approach_send():
    """
    Cron: every 15 min (cron-job.org).
    T-6hr step: LINE the assigned driver the GPS tracking link.

    Window: pickup between now+5h45m and now+6h15m (ICT), PLUS a
    catch-up rule — any unsent booking whose T-6 moment already
    passed but pickup is still >1hr away (late driver assignment,
    server downtime). Combined: now+1h < pickup <= now+6h15m.

    Idempotency: Approach_Link_Sent is checked before every send
    and set to 'Yes' only after a successful LINE push, so re-runs
    and overlap with the legacy n8n workflow are safe.

    ?dry_run=true — return JSON of what WOULD be sent; sends
    nothing, writes nothing.
    """
    dry_run = (request.args.get("dry_run") or "").strip().lower() in ("true", "1", "yes")

    now = _now_ict()
    today = now.strftime("%Y-%m-%d")
    tomorrow = (now + timedelta(days=1)).strftime("%Y-%m-%d")

    # Union of main T-6 window (now+345m..now+375m) and catch-up
    window_floor = now + timedelta(minutes=60)
    window_end = now + timedelta(minutes=375)

    fields = (
        "id,Name,Last_Name,Tour_Date,Pickup_Date_Time,Type_of_Package,"
        "Transfer_Route,Pickup_Location,Dropoff_Location,Provider_List,"
        "Approach_Link_Sent,Chanel_of_booking,Status"
    )

    # Cache-first (Stage 1.2); fallback keeps the same proven criteria
    # shape as the n8n workflow; picklist values (Status) are filtered in
    # Python either way — REST criteria on picklists is unreliable.
    records = _cache_window([today, tomorrow], package="Private Transfer")
    if records is not None:
        logger.info(f"[APPROACH-SEND] cache returned {len(records)} records")
    else:
        records = []
        for day in [today, tomorrow]:
            criteria = (
                "(Type_of_Package:equals:Private Transfer)"
                f"and(Tour_Date:equals:{day})"
            )
            records.extend(zoho_search("Koh_Chang_Orders", criteria, fields))
        logger.info(f"[APPROACH-SEND] REST search returned {len(records)} records")

    sent = 0
    candidates = []
    skipped_outsourced = 0
    skipped_no_uid = 0
    skipped_guard = 0
    errors = 0
    seen_ids = set()

    for b in records:
        bid = b.get("id")
        if not bid or bid in seen_ids:
            continue
        seen_ids.add(bid)

        if (b.get("Status") or "").strip() != "Confirmed":
            continue

        # Exclude TEST bookings (same as sibling endpoints)
        if (b.get("Chanel_of_booking") or "").strip().upper() == "TEST":
            continue

        # Idempotency guard — also what makes n8n overlap safe
        if (b.get("Approach_Link_Sent") or "") == "Yes":
            continue

        prov_ref = b.get("Provider_List")
        prov_id = prov_ref.get("id") if isinstance(prov_ref, dict) else None
        if not prov_id:
            continue

        pickup_dt = _parse_pickup_dt(b.get("Pickup_Date_Time") or "")
        if pickup_dt is None:
            continue
        if pickup_dt <= window_floor or pickup_dt > window_end:
            continue

        # ── Provider guard (backup safety net, same as siblings) ──
        blocked, block_reason = should_block(
            provider_id=prov_id,
            booking={"Type_of_Package": b.get("Type_of_Package")},
        )
        if blocked:
            prov_name = prov_ref.get("name", "") if isinstance(prov_ref, dict) else ""
            logger.warning(f"[GUARD] Blocked approach-send for {bid}: {block_reason}")
            if not dry_run:
                alert_pa_blocked(block_reason, booking_id=bid, provider_name=prov_name)
            skipped_guard += 1
            continue

        provider = _fetch_provider(prov_id)
        if provider is None:
            # Zoho hiccup — leave unsent, next run retries naturally
            errors += 1
            continue

        prov_name = (provider.get("Name") or "").strip()
        line_uid = (provider.get("Line_User_ID") or "").strip()

        # Outsourced agents (e.g. Garfield) coordinate their own
        # drivers — a GPS link would trigger false watchdog alerts.
        if provider.get("Outsourced_Agent") is True:
            logger.info(
                f"[APPROACH-SEND] Skipped {bid} — provider {prov_name} "
                "is Outsourced_Agent"
            )
            skipped_outsourced += 1
            continue

        pickup_hhmm = pickup_dt.strftime("%H:%M")
        name = (b.get("Name") or b.get("Last_Name") or "Unknown").strip()
        route = b.get("Transfer_Route") or "No route"

        if not line_uid:
            logger.warning(
                f"[APPROACH-SEND] No Line_User_ID for provider {prov_name} "
                f"(booking {bid}) — alerting ops group"
            )
            skipped_no_uid += 1
            if not dry_run and bid not in _no_uid_alerted:
                heads_up = (
                    "⚠️ ส่งลิงก์ GPS "
                    "ไม่ได้ — driver ไม่"
                    "มี LINE UID\n\n"
                    f"\U0001f516 {name}\n"
                    f"⏰ Pickup: {pickup_hhmm}\n"
                    f"\U0001f4cd {route}\n"
                    f"\U0001f690 Provider: {prov_name}\n\n"
                    "\U0001f4de กรุณาโทรหา"
                    " driver โดยตรง"
                )
                if _push_line_group(heads_up):
                    _no_uid_alerted.add(bid)
            continue

        msg = _build_approach_message(b)

        if dry_run:
            candidates.append({
                "booking_id": bid,
                "name": name,
                "pickup": b.get("Pickup_Date_Time"),
                "route": route,
                "provider": prov_name,
                "line_uid": line_uid,
                "message": msg,
            })
            continue

        # Push first, flag second — a failed push leaves
        # Approach_Link_Sent null so the next run retries.
        ok = _push_line_uid(line_uid, msg)
        if not ok:
            ok = _push_line_uid(line_uid, msg)  # retry once
        if not ok:
            logger.error(
                f"[APPROACH-SEND] LINE push FAILED twice for booking {bid} "
                f"(provider {prov_name}, uid {line_uid}) — will retry next run"
            )
            errors += 1
            continue

        if _flag_record(bid, "Approach_Link_Sent", "Yes"):
            sent += 1
        else:
            # Sent but not flagged — next run would double-send.
            logger.error(
                f"[APPROACH-SEND] CRITICAL: link sent for {bid} but "
                "Approach_Link_Sent write-back FAILED — fix in Zoho manually "
                "to avoid a duplicate send"
            )
            errors += 1

    logger.info(
        f"[APPROACH-SEND] dry_run={dry_run} sent={sent} "
        f"candidates={len(candidates)} outsourced={skipped_outsourced} "
        f"no_uid={skipped_no_uid} guard={skipped_guard} errors={errors}"
    )

    # ── Customer tracking email (~45 min before pickup), same pass ──
    # Isolated so it can never break the driver link sends above.
    try:
        email_stats = _customer_email_pass(dry_run)
    except Exception as e:
        logger.error(f"[CUST-EMAIL] Pass crashed: {e}", exc_info=True)
        email_stats = {"error": str(e)}

    result = {
        "status": "ok",
        "dry_run": dry_run,
        "checked": len(seen_ids),
        "sent": sent,
        "skipped_outsourced": skipped_outsourced,
        "skipped_no_uid": skipped_no_uid,
        "skipped_guard": skipped_guard,
        "errors": errors,
        "customer_email": {k: v for k, v in email_stats.items()
                           if dry_run or k != "candidates"},
    }
    if dry_run:
        result["candidates"] = candidates
    return jsonify(result)


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
        # ── Provider guard (backup safety net) ──
        prov = b.get("Provider_List")
        prov_id = prov.get("id") if isinstance(prov, dict) else None
        blocked, block_reason = should_block(
            provider_id=prov_id,
            booking={"Type_of_Package": b.get("Type_of_Package")},
        )
        if blocked:
            prov_name = prov.get("name", "") if isinstance(prov, dict) else ""
            logger.warning(f"[GUARD] Blocked watchdog-soft for {b.get('id')}: {block_reason}")
            alert_pa_blocked(block_reason, booking_id=b.get("id"), provider_name=prov_name)
            continue

        pickup_dt = _parse_pickup_dt(b.get("Pickup_Date_Time") or "")
        pickup_time = pickup_dt.strftime("%H:%M") if pickup_dt else "?"
        name = (b.get("Name") or b.get("Last_Name") or "Unknown").strip()
        route = b.get("Transfer_Route") or "No route"
        provider = ""
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

        # Known tracker-app user gone silent? Add context line.
        # (Drivers who never had the app get no extra line.)
        try:
            from gps_ingest import code_for_provider_id, has_tracked, get_last_seen
            code = code_for_provider_id(prov_id)
            if code and has_tracked(code):
                last_seen = get_last_seen(code)
                silent_min = int(
                    (datetime.now(timezone.utc) - last_seen).total_seconds() // 60
                )
                if silent_min >= 30:
                    msg += (
                        f"\n\n\U0001f4e1 คนขับมี"
                        f" tracker app ({code}) แต่ขาด"
                        f"สัญญาณ {silent_min} "
                        f"นาที — เช็ค"
                        f"ว่าเปิดแอป"
                        f"อยู่ไหม"
                    )
        except Exception as e:
            logger.error(f"[WATCHDOG-SOFT] Tracker-status check failed: {e}")

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

    P0-1 (15 Aug incident review): PROPOSE-ONLY. Posts ONE card to the
    team ("GPS เงียบ — ประกาศหาคนขับใหม่ไหม? [ใช่][ไม่]"). It NEVER calls
    the DM webhook itself — a confirmed provider is never replaced
    without a human tap. The [ใช่] postback relays through
    transfer-line-webhook to /rebroadcast/decision below, which calls
    DM V1 with manual_approved=true (the gate honors that flag for its
    <12h rule only).

    Loop guard: Assignment_Status must NOT be 'broadcasting'.
    Dedupe: critical_alerts row rbprop:<booking_id> = one card ever.
    """
    now = _now_ict()
    today = now.strftime("%Y-%m-%d")
    tomorrow = (now + timedelta(days=1)).strftime("%Y-%m-%d")

    # REST search: soft-alerted bookings with approach link sent
    fields = (
        "id,Name,Last_Name,Pickup_Date_Time,Transfer_Route,"
        "Provider_List,Assignment_Status,Chanel_of_booking,"
        "Approach_Acknowledged,Type_of_Package"
    )

    records = []
    for day in [today, tomorrow]:
        criteria = (
            "(Approach_Link_Sent:equals:Yes)"
            "and(Approach_Soft_Alerted:equals:Yes)"
            f"and(Tour_Date:equals:{day})"
        )
        records.extend(zoho_search("Koh_Chang_Orders", criteria, fields))

    logger.info(f"[WATCHDOG-REBROADCAST] REST search returned {len(records)} records")

    # Time window: pickup in 4hr45min to 5hr15min from now
    window_start = now + timedelta(minutes=285)
    window_end = now + timedelta(minutes=315)

    rebroadcast_count = 0
    skipped_broadcasting = 0

    for r in records:
        # Python filter: Private Transfer only
        if (r.get("Type_of_Package") or "") != "Private Transfer":
            continue

        # ── Provider guard (backup safety net) ──
        prov_rb = r.get("Provider_List")
        prov_rb_id = prov_rb.get("id") if isinstance(prov_rb, dict) else None
        blocked_rb, block_rb_reason = should_block(
            provider_id=prov_rb_id,
            booking={"Type_of_Package": r.get("Type_of_Package")},
        )
        if blocked_rb:
            prov_rb_name = prov_rb.get("name", "") if isinstance(prov_rb, dict) else ""
            logger.warning(f"[GUARD] Blocked rebroadcast for {r.get('id')}: {block_rb_reason}")
            alert_pa_blocked(block_rb_reason, booking_id=r.get("id"), provider_name=prov_rb_name)
            continue

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

        # \u2500\u2500 P0-1 (15 Aug incident review): PROPOSE ONLY, never auto-assign \u2500\u2500
        # The system may suggest a rebroadcast; only a human tap [\u0e43\u0e0a\u0e48]
        # actually triggers DM V1 (via /rebroadcast/decision with
        # manual_approved). A confirmed provider is never replaced
        # automatically.

        # Dedupe: one proposal card per booking, ever
        try:
            from db import _get_pool
            pool = _get_pool()
            with pool.connection() as conn:
                if conn.execute(
                        "SELECT 1 FROM critical_alerts WHERE alert_key = %s",
                        (f"rbprop:{booking_id}",)).fetchone():
                    continue
        except Exception as e:
            logger.error(f"[WATCHDOG-REBROADCAST] dedupe check failed {booking_id}: {e}")
            continue  # no dedupe = no send; better silent than a repeat storm

        bubble = {
            "type": "bubble",
            "header": {"type": "box", "layout": "vertical",
                       "backgroundColor": "#f59e0b", "paddingAll": "12px",
                       "contents": [{"type": "text", "size": "md", "weight": "bold",
                                     "color": "#ffffff", "wrap": True,
                                     "text": "\ud83d\udd04 GPS \u0e40\u0e07\u0e35\u0e22\u0e1a \u0e43\u0e01\u0e25\u0e49\u0e16\u0e36\u0e07\u0e07\u0e32\u0e19 \u2014 \u0e1b\u0e23\u0e30\u0e01\u0e32\u0e28\u0e2b\u0e32\u0e04\u0e19\u0e02\u0e31\u0e1a\u0e43\u0e2b\u0e21\u0e48\u0e44\u0e2b\u0e21\u0e04\u0e30?"}]},
            "body": {"type": "box", "layout": "vertical", "spacing": "sm", "contents": [
                {"type": "text", "wrap": True, "size": "sm",
                 "text": f"\ud83d\udd16 \u0e04\u0e38\u0e13{name}"},
                {"type": "text", "wrap": True, "size": "sm",
                 "text": f"\u23f0 Pickup {pickup_time} (\u0e2d\u0e35\u0e01\u0e1b\u0e23\u0e30\u0e21\u0e32\u0e13 5 \u0e0a\u0e21.)"},
                {"type": "text", "wrap": True, "size": "sm", "text": f"\ud83d\udccd {route}"},
                {"type": "text", "wrap": True, "size": "xs", "color": "#666666",
                 "text": "\u0e04\u0e19\u0e02\u0e31\u0e1a\u0e17\u0e35\u0e48\u0e22\u0e37\u0e19\u0e22\u0e31\u0e19\u0e44\u0e27\u0e49\u0e22\u0e31\u0e07\u0e44\u0e21\u0e48\u0e15\u0e2d\u0e1a\u0e41\u0e25\u0e30\u0e44\u0e21\u0e48\u0e21\u0e35\u0e2a\u0e31\u0e0d\u0e0d\u0e32\u0e13 GPS "
                         "\u0e17\u0e35\u0e21\u0e07\u0e32\u0e19\u0e08\u0e30\u0e44\u0e21\u0e48\u0e40\u0e1b\u0e25\u0e35\u0e48\u0e22\u0e19\u0e04\u0e19\u0e02\u0e31\u0e1a\u0e40\u0e2d\u0e07\u0e42\u0e14\u0e22\u0e2d\u0e31\u0e15\u0e42\u0e19\u0e21\u0e31\u0e15\u0e34 \u2014 "
                         "\u0e01\u0e14\u0e40\u0e25\u0e37\u0e2d\u0e01\u0e14\u0e49\u0e32\u0e19\u0e25\u0e48\u0e32\u0e07\u0e44\u0e14\u0e49\u0e40\u0e25\u0e22\u0e04\u0e48\u0e30"},
            ]},
            "footer": {"type": "box", "layout": "vertical", "spacing": "sm", "contents": [
                {"type": "button", "style": "primary", "color": "#f59e0b",
                 "action": {"type": "postback",
                            "label": "\u0e43\u0e0a\u0e48 \u0e1b\u0e23\u0e30\u0e01\u0e32\u0e28\u0e2b\u0e32\u0e04\u0e19\u0e02\u0e31\u0e1a\u0e43\u0e2b\u0e21\u0e48",
                            "data": f"action=rebroadcast_go&key={booking_id}",
                            "displayText": "\u0e43\u0e0a\u0e48 \u0e1b\u0e23\u0e30\u0e01\u0e32\u0e28\u0e2b\u0e32\u0e04\u0e19\u0e02\u0e31\u0e1a\u0e43\u0e2b\u0e21\u0e48"}},
                {"type": "button", "style": "secondary",
                 "action": {"type": "postback",
                            "label": "\u0e44\u0e21\u0e48 \u0e43\u0e0a\u0e49\u0e04\u0e19\u0e02\u0e31\u0e1a\u0e40\u0e14\u0e34\u0e21\u0e15\u0e48\u0e2d",
                            "data": f"action=rebroadcast_no&key={booking_id}",
                            "displayText": "\u0e44\u0e21\u0e48 \u0e43\u0e0a\u0e49\u0e04\u0e19\u0e02\u0e31\u0e1a\u0e40\u0e14\u0e34\u0e21\u0e15\u0e48\u0e2d"}},
            ]},
        }
        if not _push_group_flex(f"GPS \u0e40\u0e07\u0e35\u0e22\u0e1a \u2014 \u0e40\u0e2a\u0e19\u0e2d\u0e1b\u0e23\u0e30\u0e01\u0e32\u0e28\u0e2b\u0e32\u0e04\u0e19\u0e02\u0e31\u0e1a\u0e43\u0e2b\u0e21\u0e48 ({name})", bubble):
            continue

        try:
            with pool.connection() as conn:
                conn.execute(
                    "INSERT INTO critical_alerts (alert_key, alert_type, "
                    "booking_id) VALUES (%s, 'rebroadcast-proposal', %s) "
                    "ON CONFLICT (alert_key) DO NOTHING",
                    (f"rbprop:{booking_id}", booking_id))
        except Exception as e:
            logger.error(f"[WATCHDOG-REBROADCAST] dedupe write failed {booking_id}: {e}")
        rebroadcast_count += 1

    logger.info(
        f"[WATCHDOG-REBROADCAST] Proposed {rebroadcast_count}, "
        f"skipped {skipped_broadcasting} already-broadcasting"
    )
    return jsonify({
        "status": "ok",
        "proposed": rebroadcast_count,
        "skipped_broadcasting": skipped_broadcasting,
    })


@approach_watchdog_bp.route("/rebroadcast/decision", methods=["POST"])
def rebroadcast_decision():
    """
    Team decision relay for a rebroadcast proposal (P0-1/P0-2).
    Body: {"booking_id": ..., "decision": "go"|"no", "by": <display name>}

    go → calls the DM V1 webhook with manual_approved=true; ONE truth is
    reported to the group and it reflects what actually happened
    (request accepted vs failed) — never "ส่งแล้ว" before the fact.
    no → records the decision; nothing else fires (dedupe row already
    prevents a second card).
    """
    cron_secret = os.environ.get("CRON_SECRET", "")
    if cron_secret and request.args.get("key", "") != cron_secret:
        return jsonify({"error": "unauthorized"}), 401
    body = request.get_json(silent=True) or {}
    bid = (body.get("booking_id") or "").strip()
    decision = (body.get("decision") or "").strip().lower()
    by = (body.get("by") or "team").strip()[:80]
    if not bid or decision not in ("go", "no"):
        return jsonify({"status": "error", "message": "need booking_id + decision go|no"}), 400

    # Idempotence: only the FIRST decision acts
    try:
        from db import _get_pool
        pool = _get_pool()
        with pool.connection() as conn:
            row = conn.execute(
                "UPDATE critical_alerts SET acked_at = now(), acked_by = %s, "
                "cleared_at = now() "
                "WHERE alert_key = %s AND acked_at IS NULL "
                "RETURNING alert_key",
                (f"{by}:{decision}", f"rbprop:{bid}")).fetchone()
        if row is None:
            return jsonify({"status": "already_decided"}), 200
    except Exception as e:
        logger.error(f"[REBROADCAST-DECISION] db error {bid}: {e}")
        return jsonify({"status": "error", "message": str(e)[:150]}), 500

    if decision == "no":
        logger.info(f"[REBROADCAST-DECISION] {bid}: declined by {by}")
        return jsonify({"status": "declined"}), 200

    # decision == go → trigger DM V1 with the manual-approval flag
    try:
        resp = requests.post(DM_WEBHOOK_URL,
                             json={"id": bid, "manual_approved": True},
                             timeout=15)
        ok = resp.status_code in (200, 201)
    except Exception as e:
        logger.error(f"[REBROADCAST-DECISION] DM webhook failed {bid}: {e}")
        ok = False
    if ok:
        _push_line_group(
            f"📣 รับคำสั่งจากคุณ{by}แล้วค่ะ — ส่งงานเข้าระบบประกาศหาคนขับใหม่แล้ว\n"
            "ระบบจะแจ้งในกลุ่มเมื่อมีคนขับกดรับงานค่ะ")
        logger.info(f"[REBROADCAST-DECISION] {bid}: GO by {by}, DM webhook accepted")
        return jsonify({"status": "sent"}), 200
    _push_line_group(
        "⚠️ ส่งคำขอประกาศหาคนขับใหม่ไม่สำเร็จค่ะ (ระบบ DM ไม่ตอบรับ) — "
        "รบกวนทีมงานจัดการเองก่อนนะคะ")
    return jsonify({"status": "dm_webhook_failed"}), 502
