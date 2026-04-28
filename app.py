import os
import logging
import threading
import requests
from flask import Flask, request, jsonify

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

        status_code, response_text = send_line_message(message)
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


# TEMPORARY — remove after group routing verification
@app.route("/test/line-push", methods=["GET"])
def test_line_push():
    """Temporary: send test message to a LINE group using KOHCHANG_LINE_TOKEN."""
    group_id = request.args.get("group_id", "")
    if not group_id:
        return jsonify({"error": "missing group_id query param"}), 400
    if not KOHCHANG_LINE_TOKEN:
        return jsonify({"error": "KOHCHANG_LINE_TOKEN not set"}), 500
    try:
        resp = requests.post(
            "https://api.line.me/v2/bot/message/push",
            headers={
                "Authorization": f"Bearer {KOHCHANG_LINE_TOKEN}",
                "Content-Type": "application/json",
            },
            json={
                "to": group_id,
                "messages": [{"type": "text", "text": "\U0001f9ea Test from Orathai \u2014 checking group routing for consolidated financial reports. Please ignore."}],
            },
            timeout=15,
        )
        return jsonify({"status_code": resp.status_code, "body": resp.json() if resp.text else {}}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


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


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
