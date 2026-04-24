import os
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta

import requests

logger = logging.getLogger(__name__)

CRONJOB_API_KEY = os.environ.get("CRONJOB_API_KEY", "")
TRANSFER_LINE_TOKEN = os.environ.get("TRANSFER_LINE_TOKEN", "")
TEAM_LINE_GROUP_ID = os.environ.get("TEAM_LINE_GROUP_ID", "")

SELF_SERVICE_NAME = "thailand-tour-daily-report"

RENDER_SERVICES = [
    {"name": "peyton-charmed-bot", "url": "https://peyton-charmed-bot.onrender.com/"},
]

ICT = timezone(timedelta(hours=7))


def fetch_cronjob_status():
    """Fetch all jobs from cron-job.org API and check their status."""
    if not CRONJOB_API_KEY:
        return None, "CRONJOB_API_KEY not set"

    try:
        res = requests.get(
            "https://api.cron-job.org/jobs",
            headers={"Authorization": f"Bearer {CRONJOB_API_KEY}"},
            timeout=10,
        )
        if res.status_code != 200:
            return None, f"API returned {res.status_code}"

        data = res.json()
        jobs = data.get("jobs", [])
        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)

        results = []
        for job in jobs:
            title = job.get("title", job.get("url", "unknown"))
            enabled = job.get("enabled", False)

            schedule = job.get("schedule", {})
            hours = [h for h in schedule.get("hours", []) if h >= 0]
            minutes = [m for m in schedule.get("minutes", []) if m >= 0]
            if hours and minutes:
                ict_hour = (hours[0] + 7) % 24
                sched_str = f"{ict_hour:02d}:{minutes[0]:02d} ICT"
            elif hours:
                ict_hour = (hours[0] + 7) % 24
                sched_str = f"{ict_hour:02d}:00 ICT"
            else:
                sched_str = "custom"

            last_status = job.get("lastStatus", None)
            last_exec_ts = job.get("lastExecution", 0)

            failed_recently = False
            if last_status is not None and last_status != 1:
                if last_exec_ts:
                    try:
                        last_dt = datetime.fromtimestamp(last_exec_ts, tz=timezone.utc)
                        if last_dt > cutoff:
                            failed_recently = True
                    except (ValueError, TypeError, OSError):
                        failed_recently = True

            results.append({
                "title": title,
                "enabled": enabled,
                "schedule": sched_str,
                "last_status": last_status,
                "failed_recently": failed_recently,
            })

        results.sort(key=lambda j: j["title"].lower())
        return results, None

    except Exception as e:
        logger.error(f"[HEALTH] cron-job.org error: {e}", exc_info=True)
        return None, str(e)


def _ping_one(svc):
    """Ping a single Render service. Any HTTP response = alive."""
    try:
        res = requests.get(svc["url"], timeout=10)
        return {
            "name": svc["name"],
            "alive": True,
            "status_code": res.status_code,
            "response_ms": int(res.elapsed.total_seconds() * 1000),
        }
    except requests.exceptions.Timeout:
        logger.error(f"[HEALTH] Ping timeout for {svc['name']}")
        return {
            "name": svc["name"],
            "alive": False,
            "status_code": 0,
            "response_ms": 0,
            "error": "timeout",
        }
    except Exception as e:
        logger.error(f"[HEALTH] Ping failed for {svc['name']}: {e}")
        return {
            "name": svc["name"],
            "alive": False,
            "status_code": 0,
            "response_ms": 0,
            "error": str(e),
        }


def ping_render_services():
    """Ping all Render services in parallel. Self is always reported as alive."""
    results = [{
        "name": SELF_SERVICE_NAME,
        "alive": True,
        "status_code": 200,
        "response_ms": 0,
        "note": "self",
    }]

    with ThreadPoolExecutor(max_workers=len(RENDER_SERVICES)) as pool:
        futures = {pool.submit(_ping_one, svc): svc for svc in RENDER_SERVICES}
        for future in as_completed(futures):
            results.append(future.result())

    results.sort(key=lambda r: r["name"])
    return results


def build_health_message(cron_jobs, cron_error, render_results):
    """Compose the LINE health check message."""
    now_ict = datetime.now(ICT).strftime("%Y-%m-%d %H:%M")
    alerts = 0

    msg = f"\U0001F3E5 Daily Health Check {now_ict}\n"
    msg += "\u2500" * 21 + "\n"

    # --- cron-job.org section ---
    if cron_error:
        msg += f"\ncron-job.org: \u26A0\uFE0F {cron_error}\n"
        alerts += 1
    elif cron_jobs is not None:
        msg += f"\ncron-job.org ({len(cron_jobs)} jobs):\n"
        for job in cron_jobs:
            if not job["enabled"]:
                icon = "\u26A0\uFE0F"
                status = "DISABLED"
                alerts += 1
            elif job["failed_recently"]:
                icon = "\u274C"
                status = "FAILED"
                alerts += 1
            else:
                icon = "\u2705"
                status = f"scheduled {job['schedule']}"
            msg += f"{icon} {job['title']} \u2014 {status}\n"

    # --- Render section ---
    msg += f"\nRender Services:\n"
    for svc in render_results:
        if svc.get("note") == "self":
            msg += f"\u2705 {svc['name']} \u2014 alive (self)\n"
        elif svc["alive"]:
            msg += f"\u2705 {svc['name']} \u2014 alive ({svc['response_ms']}ms)\n"
        else:
            err = svc.get("error", f"HTTP {svc['status_code']}")
            msg += f"\u274C {svc['name']} \u2014 DOWN ({err})\n"
            alerts += 1

    # --- Summary ---
    msg += "\n"
    if alerts == 0:
        msg += "Status: \u2705 All systems normal"
    else:
        msg += f"Status: \u26A0\uFE0F {alerts} alert{'s' if alerts != 1 else ''} \u2014 check dashboard"
    msg += "\n" + "\u2500" * 21

    return msg, alerts


def send_health_line(message):
    """Send message to Team LINE group using TRANSFER_LINE_TOKEN."""
    if not TRANSFER_LINE_TOKEN:
        logger.error("[HEALTH] TRANSFER_LINE_TOKEN not set")
        return 400, "TRANSFER_LINE_TOKEN not set"
    if not TEAM_LINE_GROUP_ID:
        logger.error("[HEALTH] TEAM_LINE_GROUP_ID not set")
        return 400, "TEAM_LINE_GROUP_ID not set"

    url = "https://api.line.me/v2/bot/message/push"
    headers = {
        "Authorization": f"Bearer {TRANSFER_LINE_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "to": TEAM_LINE_GROUP_ID,
        "messages": [{"type": "text", "text": message}],
    }
    try:
        res = requests.post(url, headers=headers, json=payload, timeout=15)
        if res.status_code != 200:
            logger.error(f"[HEALTH] LINE push error: {res.status_code}: {res.text}")
        else:
            logger.info("[HEALTH] LINE push OK to Team group")
        return res.status_code, res.text
    except Exception as e:
        logger.error(f"[HEALTH] LINE push exception: {e}")
        return 500, str(e)


def run_health_check():
    """Run all checks and return (message, summary_dict)."""
    cron_jobs, cron_error = fetch_cronjob_status()
    render_results = ping_render_services()
    message, alerts = build_health_message(cron_jobs, cron_error, render_results)

    summary = {
        "cron_jobs": len(cron_jobs) if cron_jobs else 0,
        "cron_error": cron_error,
        "render_services": len(render_results),
        "render_alive": sum(1 for s in render_results if s["alive"]),
        "alerts": alerts,
    }
    return message, summary
