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
    """Returns 200 immediately, runs reconciliation in background thread."""
    def _run():
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
        except Exception as e:
            logger.error(f"[CRON] Reconciliation error: {e}", exc_info=True)

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"status": "ok", "message": "Daily reconciliation triggered"}), 200


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


@app.route("/cron/daily-payments", methods=["GET"])
def cron_daily_payments():
    """Returns 200 immediately, runs payment register in background thread."""
    def _run():
        try:
            from payments import run_daily_payments
            from line_sender import send_line_message

            logger.info("[CRON] Starting daily payments register...")
            message, stats = run_daily_payments()

            status_code, response_text = send_line_message(message, group_id=PAYMENTS_LINE_GROUP_ID)
            logger.info(
                f"[CRON] Payments LINE push status: {status_code}, "
                f"orders_due: {stats['orders_due_today']}, "
                f"message length: {len(message)}"
            )
        except Exception as e:
            logger.error(f"[CRON] Payments error: {e}", exc_info=True)

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"status": "ok", "message": "Daily payments register triggered"}), 200


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


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
