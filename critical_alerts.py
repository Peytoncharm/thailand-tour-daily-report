"""
critical_alerts.py — CRITICAL tier, Step 7b partial (ack + repeat)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Orathai requirement (12 Aug): critical alerts must repeat until a human
acts — never fire once and go quiet. Two dangerous states:

  missed-pickup : pickup time passed (10-min grace), no arrival detected,
                  our job (Private Transfer)
  driver-silent : assigned tracked driver quiet > 20 min while inside the
                  active job window (T-60 min .. pickup+90 min, not arrived)

Behaviour: LINE flex to the team group with an acknowledge button
("รับทราบ — กำลังจัดการ"), repeated every ~10 min until someone taps
acknowledge OR the state clears itself (driver arrives / GPS resumes).
Ack stops repeats immediately; who + when is logged in critical_alerts.

The button postback travels Transfer OA → transfer-line-webhook, which
calls POST /alerts/ack here. Voice-call escalation stays design-only
(D8/D9 pending). The 30-min confirm-card ⏰ is separate and unchanged.

Endpoints:
  /cron/critical-alerts?key=…   — every 10 min (n8n schedule)
                                  ?dry_run=true → detect only, no sends
                                  ?synthetic=1  → inject one fake state
                                  (self-clears on the next pass)
  /alerts/ack?key=…             — POST {alert_key, acked_by}
"""

import logging
import os
from datetime import datetime, timedelta, timezone

import requests
from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)

critical_bp = Blueprint("critical_alerts", __name__)

ICT = timezone(timedelta(hours=7))

TRANSFER_LINE_TOKEN = os.environ.get("TRANSFER_LINE_TOKEN", "")
TEAM_LINE_GROUP_ID = os.environ.get("TEAM_LINE_GROUP_ID", "")

SILENT_AFTER_S = 1200          # 20 min — matches the board's red tier
REPEAT_AFTER_S = 540           # re-send if last send ≥9 min ago (10-min cron)
GRACE_S = 600                  # same 10-min pickup grace as the dashboard
WINDOW_BEFORE_S = 3600         # silent check from T-60 min
WINDOW_AFTER_S = 90 * 60       # …until pickup+90 (missed-pickup owns later)
NEVER_STARTED_S = 6 * 3600     # no ping for 6h+ = "GPS not turned on"
AMBER_FIXED_LEAD_S = 7200      # no-GPS amber pre-warning from T-120…
AMBER_BUFFER_S = 1800          # …or (route drive time + 30 min) when known


def _fmt_mins(m):
    return f"{m // 60} ชม. {m % 60} นาที" if m >= 90 else f"{m} นาที"


def _thai_person(name):
    name = (name or "").strip()
    if not name or name.startswith("คุณ"):
        return name or "-"
    return f"คุณ{name}"


# ─────────────────────────────────────────────────────────────
# Detection
# ─────────────────────────────────────────────────────────────

def _detect_states():
    """Return {alert_key: state} for both dangerous states, computed from
    booking_cache + eta_history (arrival) + driver_latest (silence)."""
    states = {}
    from db import _get_pool
    pool = _get_pool()
    if pool is None:
        return states
    now = datetime.now(timezone.utc)
    today = datetime.now(ICT).strftime("%Y-%m-%d")
    with pool.connection() as conn:
        rows = conn.execute(
            "SELECT booking_id, pickup_ts, driver_id, provider_id, "
            "payload->>'Name', payload->>'Last_Name', "
            "payload->>'Pickup_Location', payload->>'Dropoff_Location', "
            "payload->>'Route_Key' "
            "FROM booking_cache WHERE tour_date = %s "
            "AND lower(coalesce(type_of_package,'')) = 'private transfer' "
            "ORDER BY pickup_ts NULLS LAST LIMIT 200",
            (today,),
        ).fetchall()
        # Distance-aware amber lead: where the ETA engine already knows a
        # route's real drive time, the no-GPS pre-warning starts at
        # (drive + 30 min) before pickup instead of the fixed T-120
        route_keys = list({r[8] for r in rows if r[8]})
        drive_by_route = {}
        if route_keys:
            for rk, avg_s in conn.execute(
                    "SELECT route_key, AVG(COALESCE(actual_sec, predicted_sec)) "
                    "FROM eta_history WHERE route_key = ANY(%s) "
                    "AND COALESCE(actual_sec, predicted_sec) IS NOT NULL "
                    "AND computed_at > now() - interval '60 days' "
                    "GROUP BY route_key", (route_keys,)).fetchall():
                if avg_s:
                    drive_by_route[rk] = float(avg_s)
        # Same arrival detection the dashboard uses (Step-2 completion)
        arrived_ids = {r[0] for r in conn.execute(
            "SELECT DISTINCT booking_id FROM eta_history "
            "WHERE actual_sec IS NOT NULL AND method LIKE 'checkpoint:%%' "
            "AND computed_at > now() - interval '48 hours'").fetchall()}
        ages = {r[0]: (now - r[1]).total_seconds() for r in conn.execute(
            "SELECT driver_id, ts FROM driver_latest").fetchall()}

    for (bid, pickup_ts, drv_code, provider_id,
         name, last_name, pickup_loc, dropoff_loc, route_key) in rows:
        if bid in arrived_ids or pickup_ts is None:
            continue
        info = {
            "booking_id": bid,
            "customer": f"{(name or '').strip()} {(last_name or '').strip()}".strip() or "-",
            "pickup_time": pickup_ts.astimezone(ICT).strftime("%H:%M"),
            "route": f"{(pickup_loc or '-').strip()[:40]} → {(dropoff_loc or '-').strip()[:40]}",
            "driver_code": drv_code,
            "provider_id": provider_id,
        }
        past_s = (now - pickup_ts).total_seconds()
        if past_s > GRACE_S:
            info["alert_type"] = "missed-pickup"
            states[f"missed:{bid}"] = info
            continue
        # silent check — only inside the active job window, tracked driver.
        # Pre-pickup silence is its OWN state (12 Aug): countdown wording,
        # and a separate alert key so an ack given at T-40 does not silence
        # a fresh silence alert after the pickup time passes — the ladder
        # re-escalates on each state transition (presilent → silent →
        # missed), never across one.
        if not drv_code:
            continue
        age = ages.get((drv_code or "").upper())
        # Never-started rung (12 Aug): zero pings ever, or nothing for
        # 6h+, is "GPS not turned on before the job" — a different
        # instruction to the team than "went quiet".
        never_started = age is None or age > NEVER_STARTED_S

        # Amber pre-warning (T-amber_lead .. T-60), no-GPS case only:
        # SINGLE alert, no button, no repeat — the red ack-repeat card
        # takes over when the booking crosses into the T-60 window.
        drive_s = drive_by_route.get(route_key) if route_key else None
        amber_lead = (drive_s + AMBER_BUFFER_S) if drive_s else AMBER_FIXED_LEAD_S
        if never_started and -amber_lead <= past_s < -WINDOW_BEFORE_S:
            info["alert_type"] = "gps-not-started-amber"
            info["never_started"] = True
            info["mins_to_pickup"] = int(-past_s // 60)
            info["drive_min"] = int(drive_s // 60) if drive_s else None
            states[f"pregps:{bid}"] = info
            continue

        if -WINDOW_BEFORE_S <= past_s <= WINDOW_AFTER_S:
            if age is None or age > SILENT_AFTER_S:
                pre = past_s < 0
                info["alert_type"] = "driver-silent-pre" if pre else "driver-silent"
                info["silent_min"] = None if age is None else int(age // 60)
                info["mins_to_pickup"] = int(-past_s // 60) if pre else None
                info["never_started"] = never_started
                states[("presilent:" if pre else "silent:") + str(bid)] = info
    return states


# ─────────────────────────────────────────────────────────────
# Send
# ─────────────────────────────────────────────────────────────

def _driver_line(info):
    name, phone = None, None
    if info.get("provider_id"):
        try:
            from gps_ingest import provider_entry_for_id
            e = provider_entry_for_id(info["provider_id"]) or {}
            name, phone = e.get("name"), e.get("phone")
        except Exception:
            pass
    if not name:
        return "ยังไม่มีคนขับ"
    return f"{_thai_person(name)}" + (f" · 📞 {phone}" if phone else "")


def _send_alert(alert_key, info, repeat_count, reescalation=None):
    amber = info["alert_type"] == "gps-not-started-amber"
    if amber:
        left = _fmt_mins(info.get("mins_to_pickup") or 0)
        header = f"🟠 ยังไม่เปิด GPS ก่อนงาน — อีก {left}ถึงเวลารับ"
    elif info["alert_type"] == "missed-pickup":
        header = "🔴 เลยเวลารับ — ยังไม่มีคนไปถึง (อาจพลาดลูกค้า!)"
    else:
        mins = info.get("silent_min")
        if info.get("never_started"):
            quiet = "ยังไม่เปิด GPS"
        elif mins is not None:
            quiet = f"เงียบ {mins} นาที"
        else:
            quiet = "ไม่มีสัญญาณเลย"
        if info["alert_type"] == "driver-silent-pre":
            left = info.get("mins_to_pickup")
            header = (f"🔴 คนขับ{quiet}ก่อนงาน — อีก {left} นาทีถึงเวลารับ "
                      f"(ต้องจัดการก่อนสาย!)")
        else:
            header = f"🔴 คนขับ{quiet} ระหว่างงาน (ต้องจัดการ!)"
    rep = f" · เตือนครั้งที่ {repeat_count}" if repeat_count > 1 else ""

    body_extra = []
    messages = []
    if reescalation:
        header = "‼️ เตือนอีกครั้ง — ยังไม่คลี่คลาย\n" + header
        body_extra.append(
            {"type": "text",
             "text": (f"รับทราบครั้งก่อนโดย {reescalation.get('by', '-')} "
                      f"แต่สถานะยังไม่คลี่คลายหลัง 30 นาที — ต้องกดรับทราบใหม่"),
             "size": "xs", "color": "#D02020", "wrap": True, "margin": "md"})
        # Mention-all first message (LINE textV2). If the OA/plan rejects
        # textV2, the fallback below re-sends the flex alone.
        messages.append({
            "type": "textV2",
            "text": "{everyone} ‼️ เตือนอีกครั้ง — ยังไม่คลี่คลาย",
            "substitution": {"everyone": {"type": "mention",
                                          "mentionee": {"type": "all"}}},
        })
    accent = "#B45309" if amber else "#D02020"
    if amber:
        drive_note = (f"เส้นทางนี้ใช้เวลาขับ ~{_fmt_mins(info['drive_min'])} · "
                      if info.get("drive_min") else "")
        note = (drive_note + "เตือนครั้งเดียว — ถ้ายังไม่เปิด GPS "
                "จะเตือนแดง (ต้องกดรับทราบ) ที่ 60 นาทีก่อนงาน")
    else:
        note = f"ระบบจะเตือนซ้ำทุก 10 นาทีจนกว่าจะมีคนกดรับทราบ{rep}"
    bubble = {
        "type": "bubble",
        "body": {"type": "box", "layout": "vertical", "spacing": "sm", "contents": [
            {"type": "text", "text": header, "weight": "bold", "size": "md",
             "color": accent, "wrap": True},
            {"type": "text", "text": f"ลูกค้า: {info['customer']} · Pickup {info['pickup_time']}",
             "size": "sm", "wrap": True},
            {"type": "text", "text": f"เส้นทาง: {info['route']}", "size": "sm", "wrap": True},
            {"type": "text", "text": f"คนขับ: {_driver_line(info)}", "size": "sm", "wrap": True},
        ] + body_extra + [
            {"type": "text", "text": note,
             "size": "xs", "color": "#999999", "wrap": True, "margin": "md"},
        ]},
    }
    if not amber:
        bubble["footer"] = {"type": "box", "layout": "vertical", "contents": [
            {"type": "button", "style": "primary", "color": accent,
             "action": {"type": "postback", "label": "รับทราบ — กำลังจัดการ",
                        "data": f"action=ack_critical&key={alert_key}",
                        "displayText": "รับทราบ — กำลังจัดการ"}},
        ]}
    flex = {
        "type": "flex",
        "altText": f"{header.splitlines()[-1]} — {info['customer']} {info['pickup_time']}"[:390],
        "contents": bubble,
    }
    messages.append(flex)

    def _push(msgs):
        return requests.post(
            "https://api.line.me/v2/bot/message/push",
            headers={"Authorization": f"Bearer {TRANSFER_LINE_TOKEN}",
                     "Content-Type": "application/json"},
            json={"to": TEAM_LINE_GROUP_ID, "messages": msgs},
            timeout=10,
        )
    try:
        resp = _push(messages)
        if resp.status_code != 200 and len(messages) > 1:
            # textV2 mention-all rejected → deliver the flex alone rather
            # than losing the re-escalation entirely
            logger.warning(f"[CRITICAL] {alert_key} mention-all rejected "
                           f"({resp.status_code} {resp.text[:120]}) — flex-only fallback")
            resp = _push([flex])
        ok = resp.status_code == 200
        logger.info(f"[CRITICAL] {alert_key} send #{repeat_count}"
                    + (" (re-escalation)" if reescalation else "")
                    + f": {resp.status_code}" + ("" if ok else f" {resp.text[:150]}"))
        return ok
    except Exception as e:
        logger.error(f"[CRITICAL] {alert_key} send error: {e}")
        return False


# ─────────────────────────────────────────────────────────────
# Cron
# ─────────────────────────────────────────────────────────────

@critical_bp.route("/cron/critical-alerts", methods=["GET", "POST"])
def cron_critical_alerts():
    dry_run = request.args.get("dry_run") == "true"
    synthetic = request.args.get("synthetic") == "1"
    try:
        states = _detect_states()
        if synthetic:
            states["missed:SYNTHETIC-TEST"] = {
                "alert_type": "missed-pickup", "booking_id": "SYNTHETIC-TEST",
                "customer": "TEST — กดรับทราบเพื่อทดสอบ", "pickup_time": "--:--",
                "route": "TEST → TEST", "driver_code": None, "provider_id": None,
            }
        from db import _get_pool
        pool = _get_pool()
        if pool is None:
            return jsonify({"ok": False, "reason": "no db"}), 500

        actions = []
        with pool.connection() as conn:
            open_rows = {r[0]: {"acked_at": r[1], "last_sent": r[2],
                                "repeat_count": r[3], "acked_by": r[4],
                                "reescalations": r[5]}
                         for r in conn.execute(
                             "SELECT alert_key, acked_at, last_sent, repeat_count, "
                             "acked_by, reescalations "
                             "FROM critical_alerts WHERE cleared_at IS NULL").fetchall()}
            now = datetime.now(timezone.utc)

            # State cleared itself (arrival / GPS resumed / booking gone)
            for key in set(open_rows) - set(states):
                actions.append({"key": key, "action": "cleared"})
                if not dry_run:
                    conn.execute("UPDATE critical_alerts SET cleared_at = now() "
                                 "WHERE alert_key = %s", (key,))

            for key, info in states.items():
                row = open_rows.get(key)
                # Amber no-GPS pre-warning is SINGLE-SHOT: one card, no
                # repeat, no re-escalation — the red T-60 card owns the
                # follow-through.
                if row and info["alert_type"] == "gps-not-started-amber":
                    actions.append({"key": key, "action": "amber_single_sent"})
                    continue
                if row and row["acked_at"]:
                    # Re-escalation rung (12 Aug): "acknowledged" buys 30
                    # minutes of ownership. State still dangerous after
                    # that → re-open with mention-all, fresh ack required,
                    # 10-min cycle resumes. Every re-open is counted.
                    ack_age = (now - row["acked_at"]).total_seconds()
                    if ack_age < 1800:
                        actions.append({"key": key, "action": "silenced_by_ack"})
                        continue
                    n_re = (row["reescalations"] or 0) + 1
                    if dry_run:
                        actions.append({"key": key, "action": "would_reescalate",
                                        "n_re": n_re})
                        continue
                    if _send_alert(key, info, 1,
                                   reescalation={"by": row["acked_by"] or "-"}):
                        conn.execute(
                            "UPDATE critical_alerts SET acked_at = NULL, "
                            "acked_by = NULL, last_sent = now(), repeat_count = 1, "
                            "reescalations = %s WHERE alert_key = %s", (n_re, key))
                        logger.warning(f"[CRITICAL] RE-ESCALATED {key} "
                                       f"(#{n_re}, prior ack by {row['acked_by']})")
                        actions.append({"key": key, "action": "reescalated",
                                        "n_re": n_re})
                    else:
                        actions.append({"key": key, "action": "reescalate_send_failed"})
                    continue
                if row and (now - row["last_sent"]).total_seconds() < REPEAT_AFTER_S:
                    actions.append({"key": key, "action": "too_soon"})
                    continue
                count = (row["repeat_count"] + 1) if row else 1
                if dry_run:
                    actions.append({"key": key, "action": "would_send", "n": count})
                    continue
                if _send_alert(key, info, count):
                    if row:
                        conn.execute(
                            "UPDATE critical_alerts SET last_sent = now(), "
                            "repeat_count = %s WHERE alert_key = %s", (count, key))
                    else:
                        conn.execute(
                            "INSERT INTO critical_alerts (alert_key, alert_type, "
                            "booking_id, driver_id) VALUES (%s, %s, %s, %s) "
                            "ON CONFLICT (alert_key) DO UPDATE SET cleared_at = NULL, "
                            "acked_at = NULL, acked_by = NULL, last_sent = now(), "
                            "repeat_count = 1",
                            (key, info["alert_type"], info["booking_id"],
                             info.get("driver_code")))
                    actions.append({"key": key, "action": "sent", "n": count})
                else:
                    actions.append({"key": key, "action": "send_failed"})
        return jsonify({"ok": True, "dry_run": dry_run,
                        "detected": len(states), "actions": actions}), 200
    except Exception as e:
        logger.error(f"[CRITICAL] cron error: {e}")
        return jsonify({"ok": False, "reason": str(e)[:200]}), 500


# ─────────────────────────────────────────────────────────────
# Acknowledge (called by transfer-line-webhook on button tap)
# ─────────────────────────────────────────────────────────────

@critical_bp.route("/alerts/ack", methods=["POST"])
def alerts_ack():
    cron_secret = os.environ.get("CRON_SECRET", "")
    if cron_secret and request.args.get("key", "") != cron_secret:
        return jsonify({"error": "unauthorized"}), 401
    body = request.get_json(silent=True) or {}
    alert_key = (body.get("alert_key") or "").strip()
    acked_by = (body.get("acked_by") or "team").strip()[:80]
    if not alert_key:
        return jsonify({"status": "error", "message": "no alert_key"}), 400
    try:
        from db import _get_pool
        pool = _get_pool()
        if pool is None:
            return jsonify({"status": "error", "message": "no db"}), 500
        with pool.connection() as conn:
            row = conn.execute(
                "SELECT acked_at, acked_by FROM critical_alerts WHERE alert_key = %s",
                (alert_key,)).fetchone()
            if row is None:
                return jsonify({"status": "unknown"}), 200
            if row[0] is not None:
                return jsonify({"status": "already", "acked_by": row[1]}), 200
            conn.execute(
                "UPDATE critical_alerts SET acked_at = now(), acked_by = %s "
                "WHERE alert_key = %s", (acked_by, alert_key))
        logger.info(f"[CRITICAL] ACK {alert_key} by {acked_by}")
        return jsonify({"status": "acked", "acked_by": acked_by}), 200
    except Exception as e:
        logger.error(f"[CRITICAL] ack error: {e}")
        return jsonify({"status": "error", "message": str(e)[:150]}), 500
