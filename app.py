import os
import logging
import threading
from flask import Flask, jsonify

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

from driver_location import driver_bp
app.register_blueprint(driver_bp)


@app.route("/", methods=["GET"])
def health():
    return jsonify({"status": "ok", "service": "thailand-tour-daily-report"}), 200


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
            message, group_id=RECONCILIATION_LINE_GROUP_ID, token=KOHCHANG_LINE_TOKEN
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


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
