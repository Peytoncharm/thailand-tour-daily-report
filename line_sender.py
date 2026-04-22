import os
import logging
import requests

logger = logging.getLogger(__name__)

PA_LINE_TOKEN = os.environ.get("PA_LINE_TOKEN", "")
PA_LINE_USER_ID = os.environ.get("PA_LINE_USER_ID", "")


def send_line_message(message: str, group_id: str = None) -> tuple:
    """Send via PA LINE OA push message. Returns (status_code, response_text).
    If group_id is provided, sends to that group instead of default PA_LINE_USER_ID.
    """
    url = "https://api.line.me/v2/bot/message/push"
    headers = {
        "Authorization": f"Bearer {PA_LINE_TOKEN}",
        "Content-Type": "application/json"
    }
    if len(message) > 4900:
        message = message[:4900] + "\n\n... (ตัดข้อความเพราะยาวเกินไป)"

    payload = {
        "to": group_id or PA_LINE_USER_ID,
        "messages": [{"type": "text", "text": message}]
    }
    try:
        res = requests.post(url, headers=headers, json=payload, timeout=15)
        if res.status_code != 200:
            logger.error(f"[LINE] Push error {res.status_code}: {res.text}")
        return res.status_code, res.text
    except Exception as e:
        logger.error(f"[LINE] Push exception: {e}")
        return 500, str(e)
