import os
import json
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import gspread
from google.oauth2.service_account import Credentials

logger = logging.getLogger(__name__)

ICT = ZoneInfo("Asia/Bangkok")

# ---------------------------------------------------------------------------
# Env vars
# ---------------------------------------------------------------------------

SHEET_ID = os.environ.get("MARKET_INTEL_SHEET_ID", "")
ORATHAI_UID = os.environ.get("ORATHAI_PERSONAL_LINE_UID", "")
KOHCHANG_TOKEN = os.environ.get("KOHCHANG_LINE_TOKEN", "")

DIESEL_DEFAULT = "40.20"
DIESEL_OVERRIDE = os.environ.get("DIESEL_PRICE_OVERRIDE", "")
DIESEL_DATE_DEFAULT = "28 Apr 2026"
DIESEL_DATE = os.environ.get("DIESEL_PRICE_DATE", DIESEL_DATE_DEFAULT)

TAB_NAME = "Market Intel Tracking"
DONE_URL_BASE = "https://thailand-tour-daily-report.onrender.com/market-intel/done"

# Column indices (1-based, matching Sheet header order)
COL_WEEK = 1
COL_STATUS = 2
COL_REMINDER_1 = 3
COL_REMINDER_2 = 4
COL_REMINDER_3 = 5
COL_REMINDER_4 = 6
COL_DONE_AT = 7
COL_NOTES = 8


# ---------------------------------------------------------------------------
# Sheet helpers
# ---------------------------------------------------------------------------

def _get_sheet_client():
    """Return gspread Worksheet for Market Intel Tracking tab, or None."""
    sa_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
    if not sa_json:
        logger.warning("[MARKET-INTEL] GOOGLE_SERVICE_ACCOUNT_JSON not set")
        return None

    # --- Diagnostic logging (remove after debug) ---
    if sa_json:
        logger.info(f"[MARKET-INTEL DEBUG] JSON length: {len(sa_json)}")
        logger.info(f"[MARKET-INTEL DEBUG] JSON first 100 chars: {sa_json[:100]}")
        logger.info(f"[MARKET-INTEL DEBUG] JSON last 100 chars: {sa_json[-100:]}")
        try:
            parsed = json.loads(sa_json)
            logger.info(f"[MARKET-INTEL DEBUG] private_key_id: {parsed.get('private_key_id', 'MISSING')}")
            logger.info(f"[MARKET-INTEL DEBUG] client_email: {parsed.get('client_email', 'MISSING')}")
            pk = parsed.get('private_key', '')
            logger.info(f"[MARKET-INTEL DEBUG] private_key length: {len(pk)}")
            logger.info(f"[MARKET-INTEL DEBUG] private_key starts with: {pk[:50]}")
            logger.info(f"[MARKET-INTEL DEBUG] private_key ends with: {pk[-50:]}")
            logger.info(f"[MARKET-INTEL DEBUG] private_key has \\n literal: {chr(92) + 'n' in pk}")
            logger.info(f"[MARKET-INTEL DEBUG] private_key has actual newlines: {chr(10) in pk}")
        except Exception as e:
            logger.error(f"[MARKET-INTEL DEBUG] JSON parse failed: {e}")
    # --- End diagnostic logging ---

    try:
        sa_info = json.loads(sa_json)
        creds = Credentials.from_service_account_info(sa_info, scopes=[
            "https://www.googleapis.com/auth/spreadsheets",
        ])
        gc = gspread.authorize(creds)
        spreadsheet = gc.open_by_key(SHEET_ID)
        ws = spreadsheet.worksheet(TAB_NAME)
        return ws
    except Exception as e:
        logger.warning(f"[MARKET-INTEL] Sheet connection failed: {e}")
        return None


def _current_iso_week():
    """Return current ISO week as 'YYYY-Www' string in ICT timezone."""
    now = datetime.now(ICT)
    year, week, _ = now.isocalendar()
    return f"{year}-W{week:02d}"


def _week_date_range(iso_week):
    """Convert 'YYYY-Www' to human-readable date range like '28 Apr - 4 May'."""
    try:
        year = int(iso_week[:4])
        week = int(iso_week[6:])
        # Monday of that ISO week
        monday = datetime.strptime(f"{year}-W{week:02d}-1", "%G-W%V-%u").date()
        sunday = monday + timedelta(days=6)
        mon_str = monday.strftime("%-d %b")
        sun_str = sunday.strftime("%-d %b")
        return f"{mon_str} - {sun_str}"
    except (ValueError, IndexError):
        return iso_week


def _get_week_row(ws, iso_week):
    """Find row for iso_week in Sheet. Returns (row_index, row_data) or (None, None)."""
    try:
        all_values = ws.get_all_values()
        for i, row in enumerate(all_values):
            if i == 0:  # skip header
                continue
            if row and row[0] == iso_week:
                row_data = {
                    "Week": row[0] if len(row) > 0 else "",
                    "Status": row[1] if len(row) > 1 else "",
                    "Reminder_1_Sent_At": row[2] if len(row) > 2 else "",
                    "Reminder_2_Sent_At": row[3] if len(row) > 3 else "",
                    "Reminder_3_Sent_At": row[4] if len(row) > 4 else "",
                    "Reminder_4_Sent_At": row[5] if len(row) > 5 else "",
                    "Done_At": row[6] if len(row) > 6 else "",
                    "Notes": row[7] if len(row) > 7 else "",
                }
                return i + 1, row_data  # 1-based row index for gspread
        return None, None
    except Exception as e:
        logger.warning(f"[MARKET-INTEL] Sheet read failed: {e}")
        return None, None


def _ensure_week_row(ws, iso_week):
    """Get or create row for iso_week. Returns (row_index, row_data)."""
    row_index, row_data = _get_week_row(ws, iso_week)
    if row_index is not None:
        return row_index, row_data

    # Create new row
    try:
        new_row = [iso_week, "pending", "", "", "", "", "", ""]
        ws.append_row(new_row, value_input_option="RAW")
        # Re-read to get the actual row index
        row_index, row_data = _get_week_row(ws, iso_week)
        if row_index is not None:
            return row_index, row_data
        logger.warning("[MARKET-INTEL] Created row but could not re-read it")
        return None, None
    except Exception as e:
        logger.warning(f"[MARKET-INTEL] Failed to create week row: {e}")
        return None, None


def _reminder_number(row_data):
    """Determine which reminder number to send next (1-4), or 5 if exhausted."""
    if not row_data:
        return 1
    for i in range(1, 5):
        if not row_data.get(f"Reminder_{i}_Sent_At", ""):
            return i
    return 5  # all 4 sent


# ---------------------------------------------------------------------------
# Diesel price
# ---------------------------------------------------------------------------

def _get_diesel_info():
    """Return (price_str, date_str) for diesel."""
    price = DIESEL_OVERRIDE if DIESEL_OVERRIDE else DIESEL_DEFAULT
    return price, DIESEL_DATE


# ---------------------------------------------------------------------------
# Message builders
# ---------------------------------------------------------------------------

def _build_reminder_message(iso_week, reminder_num, diesel, diesel_date):
    """Build the LINE reminder message based on reminder number."""
    date_range = _week_date_range(iso_week)
    done_url = f"{DONE_URL_BASE}?week={iso_week}"

    if reminder_num == 1:
        return (
            f"\U0001f4ca Weekly Market Intel \u2014 \u0e2a\u0e31\u0e1b\u0e14\u0e32\u0e2b\u0e4c {date_range}\n"
            f"\n"
            f"\U0001f321 \u0e2a\u0e23\u0e38\u0e1b\u0e08\u0e32\u0e01\u0e2a\u0e31\u0e1b\u0e14\u0e32\u0e2b\u0e4c\u0e01\u0e48\u0e2d\u0e19:\n"
            f"   - \u0e22\u0e31\u0e07\u0e44\u0e21\u0e48\u0e21\u0e35\u0e02\u0e49\u0e2d\u0e21\u0e39\u0e25 (\u0e2a\u0e31\u0e1b\u0e14\u0e32\u0e2b\u0e4c\u0e41\u0e23\u0e01)\n"
            f"\n"
            f"\U0001f4ca \u0e15\u0e31\u0e27\u0e40\u0e25\u0e02\u0e2a\u0e14:\n"
            f"   \u26fd Diesel: \u0e3f{diesel}/L (as of {diesel_date})\n"
            f"\n"
            f"\U0001f4cc \u0e17\u0e33\u0e40\u0e25\u0e22 (15-20 \u0e19\u0e32\u0e17\u0e35):\n"
            f"   1\ufe0f\u20e3 \u0e40\u0e1b\u0e34\u0e14 LINE driver group \u2192 screenshot 5-10 \u0e2d\u0e31\u0e19\n"
            f"   2\ufe0f\u20e3 \u0e40\u0e1b\u0e34\u0e14 FB driver group \u2192 screenshot 5-10 \u0e2d\u0e31\u0e19\n"
            f"   3\ufe0f\u20e3 \u0e40\u0e1b\u0e34\u0e14 Claude \u2192 upload screenshot\n"
            f"   4\ufe0f\u20e3 \u0e1e\u0e34\u0e21\u0e1e\u0e4c: \"Weekly market intel \u2014 \u0e2a\u0e31\u0e1b\u0e14\u0e32\u0e2b\u0e4c {date_range}\"\n"
            f"\n"
            f"\u2705 \u0e17\u0e33\u0e40\u0e2a\u0e23\u0e47\u0e08\u0e41\u0e25\u0e49\u0e27 \u0e41\u0e15\u0e30\u0e25\u0e34\u0e07\u0e01\u0e4c\u0e19\u0e35\u0e49:\n"
            f"   {done_url}\n"
            f"\n"
            f"(reminder \u0e04\u0e23\u0e31\u0e49\u0e07\u0e17\u0e35\u0e48 1 / 4)"
        )

    if reminder_num == 2:
        return (
            f"\U0001f514 Weekly Market Intel \u2014 \u0e22\u0e31\u0e07\u0e44\u0e21\u0e48\u0e44\u0e14\u0e49\u0e17\u0e33?\n"
            f"\n"
            f"\u0e2a\u0e31\u0e1b\u0e14\u0e32\u0e2b\u0e4c {date_range} \u2014 \u0e22\u0e31\u0e07\u0e23\u0e2d market data\n"
            f"\u0e43\u0e0a\u0e49\u0e40\u0e27\u0e25\u0e32\u0e41\u0e04\u0e48 15-20 \u0e19\u0e32\u0e17\u0e35\n"
            f"\n"
            f"\u2705 \u0e17\u0e33\u0e40\u0e2a\u0e23\u0e47\u0e08\u0e41\u0e25\u0e49\u0e27:\n"
            f"   {done_url}\n"
            f"\n"
            f"(reminder \u0e04\u0e23\u0e31\u0e49\u0e07\u0e17\u0e35\u0e48 2 / 4)"
        )

    if reminder_num == 3:
        return (
            f"\u26a0\ufe0f Weekly Market Intel \u2014 Deadline \u0e1e\u0e23\u0e38\u0e48\u0e07\u0e19\u0e35\u0e49\n"
            f"\n"
            f"\u0e2a\u0e31\u0e1b\u0e14\u0e32\u0e2b\u0e4c {date_range} \u2014 \u0e2a\u0e48\u0e07 report \u0e01\u0e48\u0e2d\u0e19\u0e2a\u0e38\u0e14\u0e2a\u0e31\u0e1b\u0e14\u0e32\u0e2b\u0e4c\n"
            f"\u0e23\u0e32\u0e22\u0e07\u0e32\u0e19\u0e04\u0e23\u0e31\u0e49\u0e07\u0e2a\u0e38\u0e14\u0e17\u0e49\u0e32\u0e22\u0e1e\u0e23\u0e38\u0e48\u0e07\u0e19\u0e35\u0e49\n"
            f"\n"
            f"\u2705 \u0e17\u0e33\u0e41\u0e25\u0e49\u0e27:\n"
            f"   {done_url}\n"
            f"\n"
            f"(reminder \u0e04\u0e23\u0e31\u0e49\u0e07\u0e17\u0e35\u0e48 3 / 4)"
        )

    # reminder_num == 4
    return (
        f"\U0001f6a8 Weekly Market Intel \u2014 \u0e04\u0e23\u0e31\u0e49\u0e07\u0e2a\u0e38\u0e14\u0e17\u0e49\u0e32\u0e22\n"
        f"\n"
        f"\u0e2a\u0e31\u0e1b\u0e14\u0e32\u0e2b\u0e4c {date_range} \u0e08\u0e30\u0e16\u0e39\u0e01 mark \"missed\"\n"
        f"\u0e16\u0e49\u0e32\u0e17\u0e33\u0e44\u0e14\u0e49\u0e27\u0e31\u0e19\u0e19\u0e35\u0e49 \u0e14\u0e35 \u0e16\u0e49\u0e32\u0e44\u0e21\u0e48\u0e17\u0e31\u0e19 \u0e23\u0e2d\u0e2a\u0e31\u0e1b\u0e14\u0e32\u0e2b\u0e4c\u0e2b\u0e19\u0e49\u0e32\n"
        f"\n"
        f"\u2705 \u0e17\u0e33\u0e40\u0e2a\u0e23\u0e47\u0e08\u0e41\u0e25\u0e49\u0e27:\n"
        f"   {done_url}\n"
        f"\n"
        f"(reminder \u0e04\u0e23\u0e31\u0e49\u0e07\u0e17\u0e35\u0e48 4 / 4 \u2014 last)"
    )


# ---------------------------------------------------------------------------
# Sheet update helpers
# ---------------------------------------------------------------------------

def _mark_reminder_sent(ws, row_index, reminder_num):
    """Write current ICT timestamp to the Reminder_N_Sent_At column."""
    col = COL_REMINDER_1 + (reminder_num - 1)  # col C=3 for #1, D=4 for #2, etc.
    now_str = datetime.now(ICT).strftime("%Y-%m-%dT%H:%M:%S+07:00")
    try:
        ws.update_cell(row_index, col, now_str)
        logger.info(f"[MARKET-INTEL] Marked Reminder_{reminder_num}_Sent_At = {now_str}")
    except Exception as e:
        logger.warning(f"[MARKET-INTEL] Failed to update Sheet reminder timestamp: {e}")


# ---------------------------------------------------------------------------
# Main orchestrators
# ---------------------------------------------------------------------------

def run_market_intel_reminder(dry_run=False):
    """Main entry point for the reminder cron. Returns result dict."""
    try:
        iso_week = _current_iso_week()
        logger.info(f"[MARKET-INTEL] Running for week {iso_week}, dry_run={dry_run}")

        # --- Sheet check ---
        ws = _get_sheet_client()
        sheet_ok = ws is not None
        row_index = None
        row_data = None

        if sheet_ok:
            row_index, row_data = _ensure_week_row(ws, iso_week)
            if row_index is None:
                logger.warning("[MARKET-INTEL] Could not read/create week row, proceeding without Sheet")
                sheet_ok = False

        # --- Check if already done ---
        if sheet_ok and row_data and row_data.get("Status") == "done":
            logger.info(f"[MARKET-INTEL] Week {iso_week} already done, skipping")
            return {"action": "skipped", "reason": "already done", "week": iso_week}

        # --- Determine reminder number ---
        reminder_num = _reminder_number(row_data) if sheet_ok else 1
        if reminder_num > 4:
            logger.info(f"[MARKET-INTEL] All 4 reminders already sent for {iso_week}")
            return {"action": "skipped", "reason": "all 4 reminders sent", "week": iso_week}

        # --- Build message ---
        diesel, diesel_date = _get_diesel_info()
        message = _build_reminder_message(iso_week, reminder_num, diesel, diesel_date)

        # --- Dry run ---
        if dry_run:
            return {
                "action": "dry_run",
                "week": iso_week,
                "reminder_num": reminder_num,
                "message": message,
                "message_length": len(message),
            }

        # --- Send LINE ---
        if not ORATHAI_UID:
            logger.error("[MARKET-INTEL] ORATHAI_PERSONAL_LINE_UID not set")
            return {"action": "error", "message": "ORATHAI_PERSONAL_LINE_UID not set"}

        from line_sender import _push_one

        status_code, response_text = _push_one(message, ORATHAI_UID, KOHCHANG_TOKEN)

        # Retry once on failure
        if status_code != 200:
            logger.warning(f"[MARKET-INTEL] LINE push failed ({status_code}), retrying once...")
            status_code, response_text = _push_one(message, ORATHAI_UID, KOHCHANG_TOKEN)
            if status_code != 200:
                logger.error(f"[MARKET-INTEL] LINE push retry failed: {status_code}: {response_text}")

        if status_code == 200:
            logger.info(f"[MARKET-INTEL] LINE push OK, reminder #{reminder_num} for {iso_week}")
        else:
            logger.error(f"[MARKET-INTEL] LINE push failed after retry: {status_code}")

        # --- Update Sheet ---
        if sheet_ok and row_index:
            _mark_reminder_sent(ws, row_index, reminder_num)

        return {
            "action": "sent",
            "week": iso_week,
            "reminder_num": reminder_num,
            "line_status": status_code,
        }

    except Exception as e:
        logger.error(f"[MARKET-INTEL] Unexpected error: {e}", exc_info=True)
        return {"action": "error", "message": str(e)}


def mark_week_done(iso_week):
    """Mark a week as done in the Sheet. Returns result dict."""
    ws = _get_sheet_client()
    if ws is None:
        return {"status": "error", "message": "Sheet unavailable"}

    row_index, row_data = _get_week_row(ws, iso_week)
    if row_index is None:
        return {"status": "error", "message": f"Week {iso_week} not found in Sheet"}

    if row_data.get("Status") == "done":
        return {"status": "already_done", "week": iso_week}

    try:
        now_str = datetime.now(ICT).strftime("%Y-%m-%dT%H:%M:%S+07:00")
        ws.update_cell(row_index, COL_STATUS, "done")
        ws.update_cell(row_index, COL_DONE_AT, now_str)
        logger.info(f"[MARKET-INTEL] Week {iso_week} marked done at {now_str}")
        return {"status": "done", "week": iso_week}
    except Exception as e:
        logger.error(f"[MARKET-INTEL] Failed to mark done: {e}")
        return {"status": "error", "message": str(e)}
