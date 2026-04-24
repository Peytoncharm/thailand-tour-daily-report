import os
import logging
import requests

logger = logging.getLogger(__name__)

PA_LINE_TOKEN = os.environ.get("PA_LINE_TOKEN", "")
PA_LINE_USER_ID = os.environ.get("PA_LINE_USER_ID", "")
PA_LINE_RECIPIENTS = os.environ.get("PA_LINE_RECIPIENTS", "")


def _get_recipients():
    """Return list of LINE User IDs from PA_LINE_RECIPIENTS, falling back to PA_LINE_USER_ID."""
    if PA_LINE_RECIPIENTS:
        return [r.strip() for r in PA_LINE_RECIPIENTS.split(",") if r.strip()]
    if PA_LINE_USER_ID:
        return [PA_LINE_USER_ID]
    return []


def _push_one(message: str, to: str, token: str = None) -> tuple:
    """Push message to a single LINE recipient. Returns (status_code, response_text)."""
    use_token = token or PA_LINE_TOKEN
    url = "https://api.line.me/v2/bot/message/push"
    headers = {
        "Authorization": f"Bearer {use_token}",
        "Content-Type": "application/json"
    }
    payload = {
        "to": to,
        "messages": [{"type": "text", "text": message}]
    }
    try:
        res = requests.post(url, headers=headers, json=payload, timeout=15)
        if res.status_code != 200:
            logger.error(f"[LINE] Push error to {to}: {res.status_code}: {res.text}")
        else:
            logger.info(f"[LINE] Push OK to {to}")
        return res.status_code, res.text
    except Exception as e:
        logger.error(f"[LINE] Push exception to {to}: {e}")
        return 500, str(e)


def send_line_message(message: str, group_id: str = None, token: str = None) -> tuple:
    """Send via LINE OA push message. Returns (status_code, response_text).
    If group_id is provided, sends to that single group.
    Otherwise sends to all recipients in PA_LINE_RECIPIENTS (or PA_LINE_USER_ID fallback).
    If token is provided, uses that instead of PA_LINE_TOKEN.
    """
    if len(message) > 4900:
        message = message[:4900] + "\n\n... (ตัดข้อความเพราะยาวเกินไป)"

    if group_id:
        return _push_one(message, group_id, token=token)

    recipients = _get_recipients()
    if not recipients:
        logger.error("[LINE] No recipients configured")
        return 400, "No recipients configured"

    results = []
    for recipient in recipients:
        status_code, response_text = _push_one(message, recipient, token=token)
        results.append((recipient, status_code, response_text))

    # Return the first failure if any, otherwise last success
    for recipient, code, text in results:
        if code != 200:
            return code, text
    return results[-1][1], results[-1][2]
