import os
import logging
import threading
import requests as _requests
from flask import Flask, jsonify, request

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# --- SECURITY: shared-secret check for cron/admin/test endpoints ---
CRON_SECRET = os.environ.get("CRON_SECRET", "")

@app.before_request
def _check_cron_secret():
    path = request.path or ""
    if path.startswith(("/cron/", "/admin/", "/test/")):
        if not CRON_SECRET:
            logger.warning(f"[SECURITY] CRON_SECRET not set — {path} is UNPROTECTED")
            return None
        if request.args.get("key", "") != CRON_SECRET:
            logger.warning(f"[SECURITY] Rejected {path} — bad or missing key")
            return jsonify({"error": "unauthorized"}), 401
    return None
# --- END SECURITY ---

from driver_location import driver_bp
app.register_blueprint(driver_bp)

from approach_watchdog import approach_watchdog_bp
app.register_blueprint(approach_watchdog_bp)

from gps_ingest import gps_bp
app.register_blueprint(gps_bp)

from customer_track import customer_bp
app.register_blueprint(customer_bp)

from db import db_bp, ensure_schema_async
app.register_blueprint(db_bp)
ensure_schema_async()  # idempotent DDL in a daemon thread; no-op without DATABASE_URL

from booking_cache import booking_cache_bp
app.register_blueprint(booking_cache_bp)

from dashboard import dashboard_bp
app.register_blueprint(dashboard_bp)

from eta_checkpoints import eta_bp
app.register_blueprint(eta_bp)

from eta_policy import eta_policy_bp
app.register_blueprint(eta_policy_bp)

from ferry_model import ferry_bp
app.register_blueprint(ferry_bp)

from alert_engine import alert_bp
app.register_blueprint(alert_bp)

from positioning_check import positioning_bp
app.register_blueprint(positioning_bp)
from critical_alerts import critical_bp
app.register_blueprint(critical_bp)

# --- Readiness gate (Task 1, 9 Aug): Render used port-open to decide
# "live", so the first requests after a deploy paid Zoho-token + provider-
# registry + DB-pool cold costs and cron cycles timed out (17:33 sweep,
# 20:45 double cron). /ready reports 200 only after the eager warm-up
# below completes; render.yaml healthCheckPath points here, so Render
# keeps the OLD instance serving until the NEW one is warm. ---
_READY = {"zoho": False, "providers": False, "db": False}

def _warmup():
    try:
        from zoho_thailand import _get_access_token
        _READY["zoho"] = bool(_get_access_token())
    except Exception as e:
        logger.error(f"[WARMUP] zoho token prefetch failed: {e}")
    try:
        from gps_ingest import _refresh_providers, _provider_cache
        _refresh_providers()
        _READY["providers"] = bool(_provider_cache["by_code"])
    except Exception as e:
        logger.error(f"[WARMUP] provider registry prefetch failed: {e}")
    try:
        if os.environ.get("DATABASE_URL"):
            from db import ensure_schema
            _READY["db"] = bool(ensure_schema())
        else:
            _READY["db"] = True  # no DB configured -> not a readiness blocker
    except Exception as e:
        logger.error(f"[WARMUP] db warmup failed: {e}")
    logger.info(f"[WARMUP] done: {_READY}")

threading.Thread(target=_warmup, daemon=True).start()


@app.route("/ready", methods=["GET"])
def ready():
    """Render health check target. Public by necessity (Render cannot
    send secrets); exposes only boolean warm-up state."""
    ok = all(_READY.values())
    return jsonify({"ready": ok, **_READY}), (200 if ok else 503)
# --- END readiness gate ---


@app.route("/", methods=["GET"])
def health():
    # "commit" = short hash of the RUNNING deploy (Render sets
    # RENDER_GIT_COMMIT). Lets anyone verify live-vs-pushed from outside —
    # added after the 4c59b21 build failure went unnoticed because no
    # public signal distinguished old code from new.
    _pp = -1
    try:
        from db import pickup_points_count
        _pp = pickup_points_count()
    except Exception:
        pass
    return jsonify({
        "status": "ok",
        "service": "thailand-tour-daily-report",
        "commit": os.environ.get("RENDER_GIT_COMMIT", "")[:7],
        "pickup_points": _pp,
    }), 200


@app.route("/cron/daily-reconciliation", methods=["GET"])
def cron_daily_reconciliation():
    """Runs reconciliation synchronously, then returns."""
    try:
        from reconciliation import fetch_today_orders, build_report, build_empty_report
        from line_sender import send_line_message

        logger.info("[CRON] Starting daily reconciliation...")
        records = fetch_today_orders()

        if records:
            message = build_report(records)
        else:
            message = build_empty_report()

        status_code, response_text = send_line_message(
            message, group_id=RECONCILIATION_LINE_GROUP_ID, token=PA_LINE_TOKEN
        )
        logger.info(f"[CRON] LINE push status: {status_code}, message length: {len(message)}")
        return jsonify({"status": "ok", "message": "Daily reconciliation triggered"}), 200
    except Exception as e:
        logger.error(f"[CRON] Reconciliation error: {e}", exc_info=True)
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/test/reconciliation", methods=["GET"])
def test_reconciliation():
    """Synchronous test endpoint — returns the report without sending to LINE."""
    try:
        from reconciliation import fetch_today_orders, build_report, build_empty_report

        records = fetch_today_orders()
        if records:
            message = build_report(records)
        else:
            message = build_empty_report()

        return jsonify({
            "status": "ok",
            "record_count": len(records),
            "message_preview": message,
            "message_length": len(message)
        }), 200
    except Exception as e:
        logger.error(f"[TEST] Error: {e}", exc_info=True)
        return jsonify({"status": "error", "message": str(e)}), 500


PAYMENTS_LINE_GROUP_ID = os.environ.get("PAYMENTS_LINE_GROUP_ID", "")
KOHCHANG_LINE_TOKEN = os.environ.get("KOHCHANG_LINE_TOKEN", "")
TEAM_LINE_GROUP_ID = os.environ.get("TEAM_LINE_GROUP_ID", "")
RECONCILIATION_LINE_GROUP_ID = os.environ.get("RECONCILIATION_LINE_GROUP_ID", "")
MONTHLY_REPORT_LINE_GROUP_ID = os.environ.get("MONTHLY_REPORT_LINE_GROUP_ID", "")
PA_LINE_TOKEN = os.environ.get("PA_LINE_TOKEN", "")


@app.route("/cron/daily-payments", methods=["GET"])
def cron_daily_payments():
    """Runs payment register synchronously, then returns."""
    try:
        from payments import run_daily_payments
        from line_sender import send_line_message

        logger.info("[CRON] Starting daily payments register...")
        message, stats = run_daily_payments()

        status_code, response_text = send_line_message(message, group_id=PAYMENTS_LINE_GROUP_ID, token=KOHCHANG_LINE_TOKEN)
        logger.info(
            f"[CRON] Payments LINE push status: {status_code}, "
            f"orders_due: {stats['orders_due_today']}, "
            f"message length: {len(message)}"
        )
        return jsonify({"status": "ok", "message": "Daily payments register triggered"}), 200
    except Exception as e:
        logger.error(f"[CRON] Payments error: {e}", exc_info=True)
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/test/daily-payments", methods=["GET"])
def test_daily_payments():
    """Synchronous test endpoint — returns the report as JSON without sending to LINE."""
    try:
        from payments import run_daily_payments

        message, stats = run_daily_payments()

        return jsonify({
            "status": "ok",
            "orders_found": stats["orders_found"],
            "orders_due_today": stats["orders_due_today"],
            "overdue": stats.get("overdue", 0),
            "providers": stats["providers"],
            "message_preview": message,
            "message_length": len(message)
        }), 200
    except Exception as e:
        logger.error(f"[TEST] Payments error: {e}", exc_info=True)
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/cron/morning-health-check", methods=["GET"])
def cron_morning_health_check():
    """Runs health check synchronously, then returns."""
    try:
        from health_check import run_health_check, send_health_line

        logger.info("[CRON] Starting morning health check...")
        message, summary = run_health_check()

        status_code, response_text = send_health_line(message)
        logger.info(
            f"[CRON] Health check LINE push status: {status_code}, "
            f"alerts: {summary['alerts']}, "
            f"message length: {len(message)}"
        )
        return jsonify({"status": "ok", "message": "Morning health check triggered"}), 200
    except Exception as e:
        logger.error(f"[CRON] Health check error: {e}", exc_info=True)
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/test/morning-health-check", methods=["GET"])
def test_morning_health_check():
    """Synchronous test endpoint — returns health report without sending to LINE."""
    try:
        from health_check import run_health_check

        message, summary = run_health_check()

        return jsonify({
            "status": "ok",
            "summary": summary,
            "message_preview": message,
            "message_length": len(message)
        }), 200
    except Exception as e:
        logger.error(f"[TEST] Health check error: {e}", exc_info=True)
        return jsonify({"status": "error", "message": str(e)}), 500


# ---------------------------------------------------------------------------
# Market Intel Reminder endpoints
# ---------------------------------------------------------------------------

@app.route("/cron/market-intel-reminder", methods=["GET"])
def cron_market_intel_reminder():
    """Production trigger — cron-job.org hits Mon-Thu 09:00 ICT."""
    try:
        from market_intel import run_market_intel_reminder

        logger.info("[CRON] Starting market intel reminder...")
        result = run_market_intel_reminder(dry_run=False)
        logger.info(f"[CRON] Market intel result: {result.get('action')}")
        return jsonify(result), 200
    except Exception as e:
        logger.error(f"[CRON] Market intel error: {e}", exc_info=True)
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/market-intel/done", methods=["GET"])
def market_intel_done():
    """Completion link — Orathai taps from LINE to mark week done."""
    from flask import request
    import re

    week = request.args.get("week", "")

    # Validate format
    if not re.match(r"^\d{4}-W\d{2}$", week):
        return _done_html("❌ ลิงก์ไม่ถูกต้อง", "Week format invalid", "#e74c3c"), 400

    try:
        from market_intel import mark_week_done

        result = mark_week_done(week)

        if result["status"] == "done":
            return _done_html(
                "✅ บันทึกแล้ว",
                f"สัปดาห์ {week} — เรียบร้อย",
                "#27ae60",
            ), 200
        elif result["status"] == "already_done":
            return _done_html(
                "✅ บันทึกไว้แล้ว",
                f"สัปดาห์ {week} — ทำไปแล้วก่อนหน้านี้",
                "#27ae60",
            ), 200
        else:
            return _done_html(
                "❌ เกิดข้อผิดพลาด",
                result.get("message", "Unknown error"),
                "#e74c3c",
            ), 500
    except Exception as e:
        logger.error(f"[MARKET-INTEL] Done endpoint error: {e}", exc_info=True)
        return _done_html("❌ เกิดข้อผิดพลาด", str(e), "#e74c3c"), 500


def _done_html(title, subtitle, color):
    """Return mobile-friendly HTML confirmation page."""
    return f"""<!DOCTYPE html>
<html lang="th">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
  body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    display: flex;
    justify-content: center;
    align-items: center;
    min-height: 100vh;
    margin: 0;
    background: #f5f5f5;
  }}
  .card {{
    text-align: center;
    padding: 2rem;
    max-width: 400px;
  }}
  .icon {{ font-size: 3rem; }}
  .title {{
    font-size: 1.5rem;
    font-weight: 600;
    color: {color};
    margin: 1rem 0 0.5rem;
  }}
  .subtitle {{
    font-size: 1rem;
    color: #666;
  }}
</style>
</head>
<body>
<div class="card">
  <div class="icon">{title[0]}</div>
  <div class="title">{title[2:].strip()}</div>
  <div class="subtitle">{subtitle}</div>
</div>
</body>
</html>"""


@app.route("/test/market-intel-reminder", methods=["GET"])
def test_market_intel_reminder():
    """Test endpoint — dry_run=true by default, dry_run=false sends LINE."""
    from flask import request

    try:
        from market_intel import run_market_intel_reminder

        dry_run_param = request.args.get("dry_run", "true").lower()
        dry_run = dry_run_param != "false"

        result = run_market_intel_reminder(dry_run=dry_run)
        return jsonify(result), 200
    except Exception as e:
        logger.error(f"[TEST] Market intel error: {e}", exc_info=True)
        return jsonify({"status": "error", "message": str(e)}), 500


# ---------------------------------------------------------------------------
# Payment Register endpoints
# ---------------------------------------------------------------------------

@app.route("/cron/daily-payment-register", methods=["GET", "POST"])
def cron_daily_payment_register():
    """Production trigger — cron-job.org hits daily 08:00 ICT."""
    try:
        from payment_register import run_payment_register
        from line_sender import _push_one

        logger.info("[CRON] Starting daily payment register...")
        message, stats = run_payment_register()

        # Split message if > 4900 chars
        if len(message) <= 4900:
            status_code, _ = _push_one(message, MONTHLY_REPORT_LINE_GROUP_ID, PA_LINE_TOKEN)
        else:
            parts = message.split("\n\u2501\u2501\u2501\u2501\u2501")
            for i, part in enumerate(parts):
                chunk = part if i == 0 else "\u2501\u2501\u2501\u2501\u2501" + part
                chunk = chunk.strip()
                if chunk:
                    status_code, _ = _push_one(chunk, MONTHLY_REPORT_LINE_GROUP_ID, PA_LINE_TOKEN)

        logger.info(f"[CRON] Payment register sent, due_today={stats['due_today']}, overdue={stats['overdue']}")
        return jsonify({"status": "ok", "stats": stats}), 200
    except Exception as e:
        logger.error(f"[CRON] Payment register error: {e}", exc_info=True)
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/test/payment-register", methods=["GET"])
def test_payment_register():
    """Test endpoint — returns JSON of what would be sent. NO LINE send."""
    try:
        from payment_register import (
            run_payment_register, _filter_unpaid, _classify_orders,
            _fetch_providers, _get_provider_info, _compute_due_date,
            _parse_date, _get_amount, ORDER_FIELDS,
        )
        from zoho_thailand import zoho_get_records
        from datetime import datetime
        from zoneinfo import ZoneInfo

        today = datetime.now(ZoneInfo("Asia/Bangkok")).date()

        # Step 1: Raw records
        all_orders = zoho_get_records("Koh_Chang_Orders", fields=ORDER_FIELDS)
        raw_count = len(all_orders)

        # Step 2: Count Pending
        pending_count = sum(
            1 for r in all_orders
            if (r.get("Provider_Payment_Status") or "").strip() == "Pending"
        )

        # Step 3: After _filter_unpaid
        unpaid = _filter_unpaid(all_orders, today)
        unpaid_count = len(unpaid)

        # Step 4: Classify
        provider_ids = set()
        for o in unpaid:
            pid, _ = _get_provider_info(o)
            if pid:
                provider_ids.add(pid)
        providers = _fetch_providers(provider_ids)
        due_today, overdue = _classify_orders(unpaid, providers, today)
        future = [o for o in unpaid if o not in due_today and o not in overdue]

        # Step 5: Sample records
        samples = []
        sample_names = ["wang", "tanja", "lucas"]
        for r in all_orders:
            name = (r.get("Name") or "").lower()
            if any(s in name for s in sample_names) and len(samples) < 3:
                pid, pname = _get_provider_info(r)
                provider = providers.get(pid, {})
                due_date = _compute_due_date(r, provider) if provider else None
                amt, _ = _get_amount(r)
                samples.append({
                    "name": r.get("Name"),
                    "tour_date": r.get("Tour_Date"),
                    "provider_payment_status": r.get("Provider_Payment_Status"),
                    "net_cost": r.get("Net_Cost"),
                    "provider_name": pname,
                    "computed_due_date": str(due_date) if due_date else None,
                    "today": str(today),
                    "classification": (
                        "due_today" if due_date == today else
                        "overdue" if due_date and due_date < today else
                        "future" if due_date and due_date > today else
                        "no_due_date"
                    ),
                    "has_provider_list": bool(r.get("Provider_List")),
                    "channel": r.get("Chanel_of_booking"),
                })

        # Normal report
        message, stats = run_payment_register()

        return jsonify({
            "status": "ok",
            "stats": stats,
            "message_preview": message,
            "message_length": len(message),
            "diagnostic": {
                "1_raw_records": raw_count,
                "2_pending_count": pending_count,
                "3_unpaid_after_filter": unpaid_count,
                "4_due_today": len(due_today),
                "4_overdue": len(overdue),
                "4_future": len(future),
                "5_samples": samples,
                "today": str(today),
            }
        }), 200
    except Exception as e:
        logger.error(f"[TEST] Payment register error: {e}", exc_info=True)
        return jsonify({"status": "error", "message": str(e)}), 500


# ---------------------------------------------------------------------------
# Monthly P&L Report endpoints
# ---------------------------------------------------------------------------

@app.route("/cron/monthly-report", methods=["GET", "POST"])
def cron_monthly_report():
    """Production trigger — cron-job.org hits 1st of each month 09:00 ICT."""
    try:
        from monthly_report import build_monthly_report
        from line_sender import _push_one

        logger.info("[CRON] Starting monthly P&L report...")
        message, stats = build_monthly_report()

        # Split message if > 4900 chars
        if len(message) <= 4900:
            status_code, _ = _push_one(message, MONTHLY_REPORT_LINE_GROUP_ID, PA_LINE_TOKEN)
        else:
            parts = message.split("\n\u2501\u2501\u2501\u2501\u2501")
            for i, part in enumerate(parts):
                chunk = part if i == 0 else "\u2501\u2501\u2501\u2501\u2501" + part
                chunk = chunk.strip()
                if chunk:
                    status_code, _ = _push_one(chunk, MONTHLY_REPORT_LINE_GROUP_ID, PA_LINE_TOKEN)

        logger.info(f"[CRON] Monthly report sent, bookings={stats.get('total_bookings', 0)}")
        return jsonify({"status": "ok", "stats": stats}), 200
    except Exception as e:
        logger.error(f"[CRON] Monthly report error: {e}", exc_info=True)
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/test/monthly-report", methods=["GET"])
def test_monthly_report():
    """Test endpoint — returns JSON preview. Accepts ?month=2026-04."""
    from flask import request

    try:
        from monthly_report import build_monthly_report

        month_str = request.args.get("month", None)
        message, stats = build_monthly_report(month_str=month_str)

        return jsonify({
            "status": "ok",
            "stats": stats,
            "message_preview": message,
            "message_length": len(message),
        }), 200
    except Exception as e:
        logger.error(f"[TEST] Monthly report error: {e}", exc_info=True)
        return jsonify({"status": "error", "message": str(e)}), 500


# ---------------------------------------------------------------------------
# Pre-Pickup Reminder (every 15 min via cron-job.org)
# ---------------------------------------------------------------------------

@app.route("/cron/pre-pickup-reminder", methods=["GET", "POST"])
def cron_pre_pickup_reminder():
    """DISABLED: V1 pre-pickup reminders replaced by n8n workflow 2LdDPFybPPSGKvfp.
    Endpoint kept alive so cron-job.org doesn't alert on 404, but does nothing."""
    logger.info("[CRON] Pre-pickup reminder V1 DISABLED — use n8n workflow instead")
    return jsonify({"status": "disabled", "message": "V1 disabled, use n8n workflow 2LdDPFybPPSGKvfp"}), 200


@app.route("/test/pre-pickup-reminder", methods=["GET"])
def test_pre_pickup_reminder():
    """Dry-run: show what would be sent without sending LINE messages."""
    try:
        from pre_pickup import run_pre_pickup
        stats = run_pre_pickup(dry_run=True)
        return jsonify({"status": "ok", "stats": stats}), 200
    except Exception as e:
        logger.error(f"[TEST] Pre-pickup reminder error: {e}", exc_info=True)
        return jsonify({"status": "error", "message": str(e)}), 500


# ---------------------------------------------------------------------------
# One-shot broadcast message endpoint (admin use only)
# ---------------------------------------------------------------------------

@app.route("/admin/send-bulk-line", methods=["POST"])
def admin_send_bulk_line():
    """Send the same LINE message to multiple recipients via TRANSFER_LINE_TOKEN.
    Body: {"recipients": ["Uabc...", ...], "message": "text"}
    """
    data = request.get_json(silent=True)
    if not data or "recipients" not in data or "message" not in data:
        return jsonify({"error": "Need recipients + message"}), 400

    token = os.environ.get("TRANSFER_LINE_TOKEN", "")
    if not token:
        return jsonify({"error": "TRANSFER_LINE_TOKEN not set"}), 500

    recipients = data["recipients"]
    msg = data["message"]
    results = []

    for uid in recipients:
        try:
            resp = _requests.post(
                "https://api.line.me/v2/bot/message/push",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                json={"to": uid, "messages": [{"type": "text", "text": msg}]},
                timeout=10,
            )
            results.append({"to": uid[:10], "status": resp.status_code})
        except Exception as e:
            results.append({"to": uid[:10], "status": "error", "detail": str(e)})

    ok_count = sum(1 for r in results if r["status"] == 200)
    return jsonify({"sent": ok_count, "total": len(recipients), "results": results}), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
