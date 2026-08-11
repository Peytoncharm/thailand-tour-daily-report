"""
positioning_check.py — Stage 2 P-D: evening positioning verification (SHADOW)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Layer 2 of the early-pickup defense (strategy doc + 10 Aug morning
decisions): for every NEXT-MORNING booking with a positioning
requirement (P-B fields) and an assigned driver, check at ~20:00 which
side of the water the driver is on.

  Correct side           -> silence, all fine
  Wrong side / no signal -> nudge stage: would-send LINE nudge to DRIVER
  Still wrong at ~21:00  -> team stage: would-send TEAM alert (hours
                            remain to reassign before ferries stop)

Ladder decisions (10 Aug, binding): nudge-before-team; duty-cycle
reality — a dark driver gets the nudge, never an instant team alert;
only the TEAM stage counts against the 20/day budget; the driver's
LINE reply closes the check without GPS (that reply channel is a LIVE
feature for later — in shadow there is nothing to reply to).

HARD SHADOW: findings are alert_log rows via alert_engine._record
(channel='shadow', 90-min dedup) with types 'positioning-nudge' /
'positioning-team'. NO send path exists. Stage picked by ICT clock
(>=20:45 -> team) or forced with ?stage=nudge|team for testing.

Cron: /cron/evening-positioning-check — Orathai schedules 20:00 + 21:00
(cron-job.org, same auth gate). Reads booking_cache + driver_latest.
"""

import logging
from datetime import datetime, timedelta, timezone

from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)

positioning_bp = Blueprint("positioning_check", __name__)

ICT = timezone(timedelta(hours=7))

# Koh Chang island bounding box — mirrors eta_checkpoints._ISL (kept
# separate to avoid an import cycle; both are shadow-tier crude boxes)
_ISL = (11.90, 12.16, 102.20, 102.45)


def _on_island(lat, lng):
    return _ISL[0] <= lat <= _ISL[1] and _ISL[2] <= lng <= _ISL[3]


@positioning_bp.route("/cron/evening-positioning-check", methods=["GET", "POST"])
def evening_positioning_check():
    now = datetime.now(ICT)
    stage = request.args.get("stage", "")
    if stage not in ("nudge", "team"):
        stage = "team" if (now.hour, now.minute) >= (20, 45) else "nudge"
    tomorrow = (now + timedelta(days=1)).strftime("%Y-%m-%d")

    checked, fine, would = 0, 0, []
    counts = {"correct_side": 0, "wrong_side": 0, "no_signal": 0,
              "unassigned": 0, "deduped": 0}
    try:
        from db import _direct_conn
        from eta_policy import no_signal
        from alert_engine import _record
        with _direct_conn("positioning-check") as conn:
            rows = conn.execute(
                "SELECT booking_id, driver_id, positioning_required, "
                "position_deadline, pickup_ts, payload->>'Pickup_Location' "
                "FROM booking_cache WHERE tour_date = %s "
                "AND positioning_required IS NOT NULL", (tomorrow,)).fetchall()
            latest = {code: (ts, lat, lng) for code, ts, lat, lng in conn.execute(
                "SELECT driver_id, ts, lat, lng FROM driver_latest").fetchall()}

        for bid, drv, req, deadline, pickup_ts, ploc in rows:
            checked += 1
            if not drv:
                counts["unassigned"] += 1
                continue  # no driver to position — a matching problem,
                          # not a positioning one; not this check's alert
            need_island = req.startswith("island")
            pos = latest.get(drv)
            age_s = None
            if pos:
                try:
                    age_s = int((datetime.now(timezone.utc) - pos[0]).total_seconds())
                except Exception:
                    pass
            if pos and not no_signal(age_s):
                on_isl = _on_island(pos[1], pos[2])
                if on_isl == need_island:
                    counts["correct_side"] += 1
                    fine += 1
                    continue
                reason = "wrong-side"
                counts["wrong_side"] += 1
            else:
                reason = "no-signal"
                counts["no_signal"] += 1

            atype = "positioning-team" if stage == "team" else "positioning-nudge"
            detail = (f"stage={stage} reason={reason} need={req} "
                      f"pickup={pickup_ts.isoformat() if pickup_ts else '-'} "
                      f"deadline={deadline.isoformat() if deadline else '-'}")
            out = {"deduped": 0}
            fresh = _record(str(bid), drv, atype, detail, out)
            counts["deduped"] += out["deduped"]
            if fresh:
                would.append({"booking_id": bid, "driver": drv, "type": atype,
                              "reason": reason, "need": req,
                              "pickup_location": (ploc or "")[:60],
                              "deadline": deadline.isoformat() if deadline else None})
    except Exception as e:
        logger.error(f"[POSITION-CHECK] failed: {e}")
        return jsonify({"ok": False, "reason": str(e)[:200]}), 500

    logger.info(f"[POSITION-CHECK] stage={stage} tomorrow={tomorrow} "
                f"checked={checked} fine={fine} would={len(would)} (shadow)")
    return jsonify({"ok": True, "shadow": True, "stage": stage,
                    "tomorrow": tomorrow, "checked": checked,
                    "counts": counts,
                    "would_send": would,
                    "note": "shadow — nothing sent; only the TEAM stage "
                            "would count against the 20/day budget"}), 200
