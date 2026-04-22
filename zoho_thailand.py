import os
import logging
import requests

logger = logging.getLogger(__name__)

TH_ZOHO_CLIENT_ID = os.environ.get("TH_ZOHO_CLIENT_ID", "")
TH_ZOHO_CLIENT_SECRET = os.environ.get("TH_ZOHO_CLIENT_SECRET", "")
TH_ZOHO_REFRESH_TOKEN = os.environ.get("TH_ZOHO_REFRESH_TOKEN", "")
ZOHO_API_BASE = "https://www.zohoapis.eu/crm/v2"
ZOHO_TOKEN_URL = "https://accounts.zoho.eu/oauth/v2/token"

_access_token = None


def _get_access_token() -> str:
    global _access_token
    if _access_token:
        return _access_token
    _access_token = None
    try:
        resp = requests.post(ZOHO_TOKEN_URL, params={
            "refresh_token": TH_ZOHO_REFRESH_TOKEN,
            "client_id": TH_ZOHO_CLIENT_ID,
            "client_secret": TH_ZOHO_CLIENT_SECRET,
            "grant_type": "refresh_token"
        }, timeout=10)
        data = resp.json()
        token = data.get("access_token", "")
        if token:
            _access_token = token
            logger.info("[ZOHO-TH] Access token obtained")
        else:
            logger.error(f"[ZOHO-TH] Token error: {data}")
        return token
    except Exception as e:
        logger.error(f"[ZOHO-TH] Token error: {e}")
        return ""


def refresh_token():
    global _access_token
    _access_token = None
    return _get_access_token()


def zoho_get_records(module: str, fields: str = None, max_pages: int = 5) -> list:
    """Fetch all records from a Zoho CRM module using paginated GET. Returns list of records."""
    global _access_token
    token = _get_access_token()
    if not token:
        return []

    all_records = []
    for page in range(1, max_pages + 1):
        params = {"page": page, "per_page": 200}
        if fields:
            params["fields"] = fields
        try:
            resp = requests.get(
                f"{ZOHO_API_BASE}/{module}",
                headers={"Authorization": f"Zoho-oauthtoken {token}"},
                params=params,
                timeout=15
            )
            if resp.status_code == 401 and page == 1:
                _access_token = None
                token = _get_access_token()
                if not token:
                    return []
                resp = requests.get(
                    f"{ZOHO_API_BASE}/{module}",
                    headers={"Authorization": f"Zoho-oauthtoken {token}"},
                    params=params,
                    timeout=15
                )
            if resp.status_code == 204:
                break
            if resp.status_code != 200:
                logger.error(f"[ZOHO-TH] Get records error {resp.status_code}: {resp.text}")
                break
            data = resp.json()
            records = data.get("data", [])
            all_records.extend(records)
            if not data.get("info", {}).get("more_records", False):
                break
        except Exception as e:
            logger.error(f"[ZOHO-TH] Get records exception: {e}")
            break

    logger.info(f"[ZOHO-TH] Fetched {len(all_records)} total records from {module} ({page} pages)")
    return all_records


def zoho_search(module: str, criteria: str, fields: str = None, page: int = 1) -> list:
    """Search a Zoho CRM module with criteria. Returns list of records."""
    global _access_token
    token = _get_access_token()
    if not token:
        return []

    params = {"criteria": criteria, "page": page, "per_page": 200}
    if fields:
        params["fields"] = fields

    try:
        resp = requests.get(
            f"{ZOHO_API_BASE}/{module}/search",
            headers={"Authorization": f"Zoho-oauthtoken {token}"},
            params=params,
            timeout=15
        )
        if resp.status_code == 401:
            _access_token = None
            token = _get_access_token()
            if not token:
                return []
            resp = requests.get(
                f"{ZOHO_API_BASE}/{module}/search",
                headers={"Authorization": f"Zoho-oauthtoken {token}"},
                params=params,
                timeout=15
            )
        if resp.status_code == 204:
            return []
        if resp.status_code != 200:
            logger.error(f"[ZOHO-TH] Search error {resp.status_code}: {resp.text}")
            return []
        data = resp.json()
        return data.get("data", [])
    except Exception as e:
        logger.error(f"[ZOHO-TH] Search exception: {e}")
        return []
