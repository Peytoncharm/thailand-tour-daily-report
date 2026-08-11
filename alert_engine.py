"""
alert_engine.py — Stage 2 Step 7 (free half): alert rules 1 + 4, SHADOW ONLY
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
The two GPS rules that need NO routing API, evaluated per checkpoint pass
(called from eta_checkpoints for each in-window booking):

  Rule 1  no-signal        — assigned, tracker-known driver with no fresh
                             position inside any T-window. Distinct type,
                             NOT 'late' (spec). Exempt while mid-crossing
                             (ferry corridor — ladder decision, 10 Aug).
  Rule 4  wrong-direction  — distance-to-pickup grew across the last two
                             checkpoint windows (spec: diverging for 2
                             consecutive checkpoints). Coarse claim —
                             allowed at every precision tier (eta_policy).

HARD SHADOW: every finding is a row in alert_log with channel='shadow'
(dedup repeats: 'shadow-suppressed'). There is NO send path in this
module at all — ALERTS_ENABLED and live delivery arrive with rules 2+3
after the Step-7 digest gate. Early-island suppression (before T-90) is
respected: suppressed bookings are not evaluated (counted for the JSON).

Dedup per spec: same (booking_id, alert_type) within DEDUP_MIN=90 min.
Budget per spec: 20/day across all types — shadow rows are counted and
the would-be-exhausted point is reported, nothing is dropped in shadow.
Rules 2 (not-departed) + 3 (at-risk) join after Step 4 (stage-2, D1).
"""

import logging

from flask import Blueprint, jsonify

logger = logging.getLogger(__name__)

alert_bp = Blueprint("alert_engine", __name__)

DEDUP_MIN = 90       # spec proposed
DAILY_BUDGET = 20    # spec, locked

WRONG_DIR_MIN_GROWTH_KM = 1.0   # distance must grow by more than this
                                # across two checkpoints to count as
                                # diverging (GPS jitter guard) [proposed]


def shadow_evaluate(b, win, drv_code, age_s, dpos, pin, dist_km):
    """Evaluate rules 1+4 for one in-window booking. Called from the
    checkpoint pass with everything it already computed — zero extra
    Zoho/GPS reads. Returns per-rule counts. Never raises."""
    out = {"no_signal": 0, "wrong_direction": 0, "deduped": 0}
    bid = str(b.get("id"))
    try:
        from eta_policy import no_signal

        # ── Rule 1: no-signal ──
        if drv_code and no_signal(age_s):
            # ferry exemption needs a position; a fully dark driver has
            # none, so the exemption can't apply there (correct: dark is
            # exactly what rule 1 exists for)
            out["no_signal"] += _record(
                bid, drv_code, "no-signal",
                f"window={win} age={'never' if age_s is None else str(age_s) + 's'}",
                out)
        elif drv_code and dpos is not None:
            try:
                from ferry_model import is_midwater, _pier_pins
                if is_midwater(dpos[0], dpos[1], _pier_pins()):
                    logger.info(f"[ALERT-SHADOW] booking={bid} driver={drv_code} "
                                f"mid-crossing — ladder exempt (shadow)")
            except Exception:
                pass

        # ── Rule 4: wrong-direction ──
        if drv_code and dpos is not None and pin is not None and dist_km is not None:
            prev = _previous_distance(bid, win)
            if prev is not None and dist_km > prev + WRONG_DIR_MIN_GROWTH_KM:
                out["wrong_direction"] += _record(
                    bid, drv_code, "wrong-direction",
                    f"window={win} dist={dist_km:.1f}km prev={prev:.1f}km",
                    out)
    except Exception as e:
        logger.warning(f"[ALERT-SHADOW] evaluate failed for {bid}: {e}")
    return out


def _previous_distance(bid, win):
    """distance_km from this booking's PREVIOUS checkpoint window today
    (the row the stage-1 filter wrote). None if no prior measurement."""
    try:
        from db import _get_pool
        pool = _get_pool()
        if pool is None:
            return None
        with pool.connection() as conn:
            row = conn.execute(
                "SELECT distance_km FROM eta_history "
                "WHERE booking_id = %s AND method LIKE 'checkpoint:%%' "
                "  AND method != %s AND distance_km IS NOT NULL "
                "  AND computed_at::date = (now() AT TIME ZONE 'Asia/Bangkok')::date "
                "ORDER BY computed_at DESC LIMIT 1",
                (bid, f"checkpoint:{win}")).fetchone()
        return float(row[0]) if row else None
    except Exception:
        return None


def _record(bid, drv, atype, detail, out):
    """Write a shadow alert row with 90-min dedup. Returns 1 when a fresh
    row was written, 0 when deduped."""
    try:
        from db import _get_pool
        pool = _get_pool()
        if pool is None:
            return 0
        with pool.connection() as conn:
            dup = conn.execute(
                "SELECT 1 FROM alert_log WHERE booking_id = %s AND alert_type = %s "
                f"AND sent_at > now() - interval '{DEDUP_MIN} minutes' LIMIT 1",
                (bid, atype)).fetchone()
            channel = "shadow-suppressed" if dup else "shadow"
            conn.execute(
                "INSERT INTO alert_log (booking_id, driver_id, alert_type, channel, detail) "
                "VALUES (%s, %s, %s, %s, %s)",
                (bid, drv, atype, channel, detail))
        if dup:
            out["deduped"] += 1
            return 0
        logger.info(f"[ALERT-SHADOW] WOULD-ALERT type={atype} booking={bid} "
                    f"driver={drv} {detail} (shadow — nothing sent)")
        return 1
    except Exception as e:
        logger.warning(f"[ALERT-SHADOW] record failed for {bid}/{atype}: {e}")
        return 0


def shadow_budget_today():
    """Would-count against the 20/day budget: fresh shadow rows today
    (dedup-suppressed repeats excluded). Reported, never enforced here."""
    try:
        from db import _get_pool
        pool = _get_pool()
        if pool is None:
            return None
        with pool.connection() as conn:
            row = conn.execute(
                "SELECT count(*) FROM alert_log WHERE channel = 'shadow' "
                "AND sent_at::date = (now() AT TIME ZONE 'Asia/Bangkok')::date"
            ).fetchone()
        return int(row[0])
    except Exception:
        return None


@alert_bp.route("/cron/alert-shadow-digest", methods=["GET", "POST"])
def alert_shadow_digest():
    """The Step-7 gate surface: today's would-have-alerted list for
    Orathai's review. Read-only."""
    rows = []
    try:
        from db import _direct_conn
        with _direct_conn("alert-digest") as conn:
            for bid, drv, atype, sent_at, channel, detail in conn.execute(
                    "SELECT booking_id, driver_id, alert_type, sent_at, channel, detail "
                    "FROM alert_log WHERE channel LIKE 'shadow%%' "
                    "AND sent_at > now() - interval '24 hours' "
                    "ORDER BY sent_at DESC LIMIT 100").fetchall():
                rows.append({"booking_id": bid, "driver": drv, "type": atype,
                             "at": sent_at.isoformat(), "channel": channel,
                             "detail": detail})
    except Exception as e:
        return jsonify({"ok": False, "reason": str(e)[:200]}), 500
    fresh = [r for r in rows if r["channel"] == "shadow"]
    return jsonify({"ok": True, "shadow": True,
                    "would_have_alerted_24h": len(fresh),
                    "budget_note": f"{len(fresh)}/{DAILY_BUDGET} would count "
                                   f"against the daily budget",
                    "rows": rows}), 200
