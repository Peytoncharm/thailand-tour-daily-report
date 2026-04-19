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


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
