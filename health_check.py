"""
health_check.py — Daily system health check (08:00 ICT)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Sends a Thai-formatted summary to PA LINE group covering:
  1. n8n workflow status (active/inactive)
  2. Render services (up/down)
  3. Last-24hr execution summary (T-6, T-30, watchdog)
  4. Errors from last 24hrs

Endpoint: /cron/morning-health-check (already wired in app.py)
"""

import os
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta

import requests

logger = logging.getLogger(__name__)

ICT = timezone(timedelta(hours=7))

# ── Env vars ──────────────────────────────────────────────────
N8N_API_KEY = os.environ.get("N8N_API_KEY", "")
N8N_BASE_URL = os.environ.get(
    "N8N_BASE_URL", "https://tourtransfer.app.n8n.cloud"
)
PA_LINE_TOKEN = os.environ.get("PA_LINE_TOKEN", "")
TRANSFER_LINE_TOKEN = os.environ.get("TRANSFER_LINE_TOKEN", "")
TEAM_LINE_GROUP_ID = os.environ.get("TEAM_LINE_GROUP_ID", "")
CRONJOB_API_KEY = os.environ.get("CRONJOB_API_KEY", "")

# ── n8n workflows to monitor ─────────────────────────────────
N8N_WORKFLOWS = [
    {"id": "9XvP8N37aj1D5brO", "label": "Driver Matching V1"},
    {"id": "QhGJYtjYjvRq1OD0", "label": "T-6 Approach Link"},
    {"id": "2LdDPFybPPSGKvfp", "label": "T-30 Pre-Pickup"},
    {"id": "MUYWOj1EMwxRHhLJ", "label": "Team Reminder"},
]

# Workflows whose executions we count (n8n-based only)
EXEC_TRACK = [
    {"id": "QhGJYtjYjvRq1OD0", "label": "T-6 ส่ง approach link"},
    {"id": "2LdDPFybPPSGKvfp", "label": "T-30 pre-pickup"},
]

# Render services to ping
RENDER_SERVICES = [
    {
        "name": "thailand-tour-daily-report",
        "url": "https://thailand-tour-daily-report.onrender.com/",
    },
    {
        "name": "transfer-line-webhook",
        "url": "https://transfer-line-webhook.onrender.com/",
    },
]

# cron-job.org keywords for watchdog crons
WATCHDOG_CRON_KEYWORDS = ["watchdog", "approach"]


# ── Helpers ───────────────────────────────────────────────────

def _n8n_get(path, params=None):
    """GET request to n8n Cloud API."""
    resp = requests.get(
        f"{N8N_BASE_URL}{path}",
        headers={"X-N8N-API-KEY": N8N_API_KEY},
        params=params,
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def _fmt_ict(iso_str):
    """Format ISO datetime to Thai-friendly ICT string."""
    if not iso_str:
        return "ไม่มีข้อมูล"
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return dt.astimezone(ICT).strftime("%d/%m %H:%M")
    except (ValueError, TypeError):
        return iso_str[:16]


# ── 1. n8n workflow status ────────────────────────────────────

def fetch_n8n_workflow_status():
    """Check active/inactive for each monitored workflow."""
    if not N8N_API_KEY:
        return None, "N8N_API_KEY not set"

    results = []
    for wf in N8N_WORKFLOWS:
        try:
            data = _n8n_get(f"/api/v1/workflows/{wf['id']}")
            results.append({
                "label": wf["label"],
                "active": data.get("active", False),
            })
        except Exception as e:
            logger.error(f"[HEALTH] n8n workflow {wf['id']} error: {e}")
            results.append({
                "label": wf["label"],
                "active": None,
                "error": str(e),
            })
    return results, None


# ── 2. Render services ────────────────────────────────────────

def _ping_one(svc):
    """Ping a single Render service."""
    try:
        resp = requests.get(svc["url"], timeout=15)
        return {
            "name": svc["name"],
            "alive": True,
            "status_code": resp.status_code,
            "response_ms": int(resp.elapsed.total_seconds() * 1000),
        }
    except Exception as e:
        logger.error(f"[HEALTH] Ping failed for {svc['name']}: {e}")
        return {"name": svc["name"], "alive": False, "error": str(e)}


def ping_render_services():
    """Ping all Render services in parallel."""
    if not RENDER_SERVICES:
        return []
    with ThreadPoolExecutor(max_workers=len(RENDER_SERVICES)) as pool:
        futures = {pool.submit(_ping_one, svc): svc for svc in RENDER_SERVICES}
        results = [f.result() for f in as_completed(futures)]
    results.sort(key=lambda r: r["name"])
    return results


# ── 3. n8n execution counts (last 24 hrs) ────────────────────

def _count_executions(workflow_id, status, cutoff_iso):
    """Count executions of a workflow with given status after cutoff."""
    count = 0
    last_at = None
    try:
        data = _n8n_get("/api/v1/executions", {
            "workflowId": workflow_id,
            "status": status,
            "limit": 250,
        })
        for ex in data.get("data", []):
            started = ex.get("startedAt", "")
            if started >= cutoff_iso:
                count += 1
                if last_at is None:
                    last_at = started
    except Exception as e:
        logger.error(
            f"[HEALTH] n8n exec count error wf={workflow_id} status={status}: {e}"
        )
    return count, last_at


def fetch_n8n_exec_summary():
    """Execution counts for tracked workflows in last 24hrs."""
    if not N8N_API_KEY:
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    cutoff_iso = cutoff.strftime("%Y-%m-%dT%H:%M:%S.000Z")

    results = []
    for wf in EXEC_TRACK:
        ok, last_ok = _count_executions(wf["id"], "success", cutoff_iso)
        err, _ = _count_executions(wf["id"], "error", cutoff_iso)
        results.append({
            "label": wf["label"],
            "success": ok,
            "error": err,
            "last_success": last_ok,
        })
    return results


# ── 4. n8n errors (all workflows, last 24 hrs) ───────────────

def fetch_n8n_errors():
    """Fetch all error executions from n8n in last 24hrs."""
    if not N8N_API_KEY:
        return [], "N8N_API_KEY not set"

    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    cutoff_iso = cutoff.strftime("%Y-%m-%dT%H:%M:%S.000Z")

    # Build workflow-ID-to-name map from monitored list
    wf_names = {wf["id"]: wf["label"] for wf in N8N_WORKFLOWS}

    errors = []
    try:
        data = _n8n_get("/api/v1/executions", {
            "status": "error",
            "limit": 100,
        })
        for ex in data.get("data", []):
            started = ex.get("startedAt", "")
            if started < cutoff_iso:
                continue
            wf_id = ex.get("workflowId", "")
            errors.append({
                "workflow": wf_names.get(wf_id, wf_id),
                "started": started,
            })
    except Exception as e:
        logger.error(f"[HEALTH] n8n errors fetch failed: {e}")
        return [], str(e)

    return errors, None


# ── 5. Watchdog (Render-based, via cron-job.org) ──────────────

def fetch_watchdog_cron_status():
    """Get last-execution status for watchdog crons from cron-job.org."""
    if not CRONJOB_API_KEY:
        return None, "CRONJOB_API_KEY not set"

    try:
        resp = requests.get(
            "https://api.cron-job.org/jobs",
            headers={"Authorization": f"Bearer {CRONJOB_API_KEY}"},
            timeout=10,
        )
        if resp.status_code != 200:
            return None, f"cron-job.org API {resp.status_code}"

        jobs = resp.json().get("jobs", [])
        results = []
        for job in jobs:
            title = (job.get("title") or job.get("url") or "").lower()
            if any(kw in title for kw in WATCHDOG_CRON_KEYWORDS):
                last_ts = job.get("lastExecution", 0)
                last_ok = job.get("lastStatus") == 1
                last_dt = None
                if last_ts:
                    try:
                        last_dt = datetime.fromtimestamp(
                            last_ts, tz=timezone.utc
                        )
                    except (ValueError, TypeError, OSError):
                        pass
                results.append({
                    "title": job.get("title", "unknown"),
                    "enabled": job.get("enabled", False),
                    "last_ok": last_ok,
                    "last_dt": last_dt,
                })
        return results, None
    except Exception as e:
        logger.error(f"[HEALTH] cron-job.org error: {e}")
        return None, str(e)


# ── Build message ─────────────────────────────────────────────

def build_health_message():
    """Compose the full Thai health-check message. Returns (msg, alerts)."""
    now_ict = datetime.now(ICT)
    alerts = 0

    # Gather data
    wf_status, wf_err = fetch_n8n_workflow_status()
    render_results = ping_render_services()
    exec_summary = fetch_n8n_exec_summary()
    n8n_errors, err_fetch_err = fetch_n8n_errors()
    watchdog_crons, wd_err = fetch_watchdog_cron_status()

    # ── Header ──
    lines = [
        f"\U0001f3e5 สรุประบบประจำวัน {now_ict.strftime('%d/%m/%Y %H:%M')}",
        "\u2501" * 22,
    ]

    # ── n8n Workflows ──
    lines.append("\n\U0001f4cb n8n Workflows:")
    if wf_err:
        lines.append(f"\u26a0\ufe0f {wf_err}")
        alerts += 1
    elif wf_status:
        for wf in wf_status:
            if wf.get("active") is None:
                icon, status = "\u26a0\ufe0f", "\u0e40\u0e0a\u0e37\u0e48\u0e2d\u0e21\u0e15\u0e48\u0e2d\u0e44\u0e21\u0e48\u0e44\u0e14\u0e49"
                alerts += 1
            elif wf["active"]:
                icon, status = "\u2705", "Active"
            else:
                icon, status = "\u274c", "Inactive"
                alerts += 1
            lines.append(f"{icon} {wf['label']} \u2014 {status}")

    # ── Render Services ──
    lines.append(f"\n\U0001f5a5\ufe0f Render Services:")
    for svc in render_results:
        if svc.get("alive"):
            lines.append(
                f"\u2705 {svc['name']} \u2014 UP ({svc['response_ms']}ms)"
            )
        else:
            lines.append(f"\u274c {svc['name']} \u2014 DOWN")
            alerts += 1

    # ── Execution Summary (24hr) ──
    lines.append(f"\n\U0001f4ca \u0e2a\u0e23\u0e38\u0e1b 24 \u0e0a\u0e21. \u0e17\u0e35\u0e48\u0e1c\u0e48\u0e32\u0e19\u0e21\u0e32:")
    for ex in exec_summary:
        s, e = ex["success"], ex["error"]
        last = _fmt_ict(ex["last_success"])
        line = f"\u2022 {ex['label']}: \u2705{s} \u0e2a\u0e33\u0e40\u0e23\u0e47\u0e08"
        if e > 0:
            line += f" / \u274c{e} error"
            alerts += 1
        lines.append(line)
        lines.append(f"  \u0e25\u0e48\u0e32\u0e2a\u0e38\u0e14: {last}")

    # ── Watchdog (Render via cron-job.org) ──
    if watchdog_crons:
        lines.append(f"\n\U0001f6a8 Watchdog (Render):")
        for wd in watchdog_crons:
            if not wd["enabled"]:
                lines.append(f"\u26a0\ufe0f {wd['title']} \u2014 DISABLED")
                alerts += 1
            elif wd["last_ok"]:
                last = (
                    wd["last_dt"].astimezone(ICT).strftime("%d/%m %H:%M")
                    if wd["last_dt"]
                    else "?"
                )
                lines.append(
                    f"\u2705 {wd['title']} \u2014 OK (\u0e25\u0e48\u0e32\u0e2a\u0e38\u0e14 {last})"
                )
            else:
                lines.append(f"\u274c {wd['title']} \u2014 FAILED")
                alerts += 1
    elif wd_err:
        lines.append(f"\n\U0001f6a8 Watchdog: \u26a0\ufe0f {wd_err}")

    # ── Errors ──
    if n8n_errors:
        lines.append(
            f"\n\u26a0\ufe0f Errors \u0e43\u0e19 24 \u0e0a\u0e21. ({len(n8n_errors)} \u0e23\u0e32\u0e22\u0e01\u0e32\u0e23):"
        )
        for err in n8n_errors[:5]:
            lines.append(
                f"\u2022 {err['workflow']} \u2014 {_fmt_ict(err['started'])}"
            )
        if len(n8n_errors) > 5:
            lines.append(
                f"  ... \u0e2d\u0e35\u0e01 {len(n8n_errors) - 5} \u0e23\u0e32\u0e22\u0e01\u0e32\u0e23"
            )
        alerts += len(n8n_errors)
    elif err_fetch_err:
        lines.append(f"\n\u26a0\ufe0f Errors: {err_fetch_err}")
    else:
        lines.append(
            "\n\u2705 \u0e44\u0e21\u0e48\u0e21\u0e35 error \u0e43\u0e19 24 \u0e0a\u0e21. \u0e17\u0e35\u0e48\u0e1c\u0e48\u0e32\u0e19\u0e21\u0e32"
        )

    # ── Footer ──
    lines.append("\n" + "\u2501" * 22)
    if alerts == 0:
        lines.append(
            "\u0e2a\u0e16\u0e32\u0e19\u0e30: \u2705 \u0e23\u0e30\u0e1a\u0e1a\u0e17\u0e31\u0e49\u0e07\u0e2b\u0e21\u0e14\u0e1b\u0e01\u0e15\u0e34"
        )
    else:
        lines.append(
            f"\u0e2a\u0e16\u0e32\u0e19\u0e30: \u26a0\ufe0f \u0e1e\u0e1a {alerts} \u0e23\u0e32\u0e22\u0e01\u0e32\u0e23\u0e15\u0e49\u0e2d\u0e07\u0e15\u0e23\u0e27\u0e08\u0e2a\u0e2d\u0e1a"
        )

    msg = "\n".join(lines)

    # LINE 5000-char cap — truncate if needed
    if len(msg) > 4900:
        msg = msg[:4850] + "\n...\u0e15\u0e31\u0e14\u0e40\u0e19\u0e37\u0e48\u0e2d\u0e07\u0e08\u0e32\u0e01\u0e02\u0e49\u0e2d\u0e04\u0e27\u0e32\u0e21\u0e22\u0e32\u0e27\u0e40\u0e01\u0e34\u0e19"

    return msg, alerts


# ── Send to LINE ──────────────────────────────────────────────

def send_health_line(message):
    """Send health-check message to PA LINE group.
    Falls back to TEAM_LINE_GROUP_ID with TRANSFER_LINE_TOKEN.
    """
    token = PA_LINE_TOKEN or TRANSFER_LINE_TOKEN
    group_id = TEAM_LINE_GROUP_ID

    if not token:
        logger.error("[HEALTH] No LINE token available (PA_LINE_TOKEN / TRANSFER_LINE_TOKEN)")
        return 400, "No LINE token"
    if not group_id:
        logger.error("[HEALTH] TEAM_LINE_GROUP_ID not set")
        return 400, "No group ID"

    try:
        resp = requests.post(
            "https://api.line.me/v2/bot/message/push",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json={
                "to": group_id,
                "messages": [{"type": "text", "text": message}],
            },
            timeout=15,
        )
        if resp.status_code != 200:
            logger.error(f"[HEALTH] LINE push error: {resp.status_code}: {resp.text}")
        else:
            logger.info("[HEALTH] Health check sent to LINE group")
        return resp.status_code, resp.text
    except Exception as e:
        logger.error(f"[HEALTH] LINE push exception: {e}")
        return 500, str(e)


# ── Main entry ────────────────────────────────────────────────

def run_health_check():
    """Run all checks and return (message, summary_dict).
    Signature kept compatible with app.py.
    """
    message, alerts = build_health_message()
    summary = {"alerts": alerts}
    return message, summary
