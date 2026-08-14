"""
positioning_live.py — LIVE evening positioning check (Orathai, 13 Aug)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"No manual night-before messages" — the system checks, asks, and
escalates by itself. Scope: TOMORROW's Private Transfer pickups before
the morning cutoff (ferry_model.json island_cutoff, default 09:00,
env POSITIONING_MORNING_CUTOFF overrides).

Nightly ~20:00 ICT (n8n schedule; second pass ~21:30):
  driver GPS fresh + correct side  -> log only, nobody messaged
  driver GPS fresh + WRONG side    -> RED ack-repeat card to the team
     IMMEDIATELY (via the critical-alerts machinery: 10-min repeats,
     ack button, 30-min re-escalation) with the ferry math
  driver GPS off/unknown (allowed  -> LINE flex to the DRIVER asking him
     by the duty-cycle policy)        to confirm his side — two buttons;
                                      his tap IS the evidence, GPS not
                                      required overnight
  no reply by ~21:30               -> amber info card to the team so
                                      they can call him tonight

Driver messages: คุณ+ชื่อ, sender voice ทีมงาน, never demand overnight
GPS. Button taps travel Transfer OA -> transfer-line-webhook
(action=pos_confirm) -> POST /positioning/answer here.

State: positioning_checks table (one row per booking per pickup-date).
Wrong-side states are exported to critical_alerts via
wrong_side_states() so repeats/ack/re-escalation come for free.
"""

import logging
import math
import os
from datetime import datetime, timedelta, timezone

import requests
from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)

positioning_live_bp = Blueprint("positioning_live", __name__)

ICT = timezone(timedelta(hours=7))

TRANSFER_LINE_TOKEN = os.environ.get("TRANSFER_LINE_TOKEN", "")
TEAM_LINE_GROUP_ID = os.environ.get("TEAM_LINE_GROUP_ID", "")

GPS_MAX_AGE_S = int(os.environ.get("POSITIONING_GPS_MAX_AGE_S", str(6 * 3600)))
FOLLOWUP_AFTER = os.environ.get("POSITIONING_FOLLOWUP_AFTER", "21:15")  # ICT HH:MM
STATE_WINDOW = ("19:30", "23:59")  # wrong-side states exported in this ICT window

# Same island bbox as positioning_check/eta_checkpoints (crude, proven)
_ISL = (11.90, 12.16, 102.20, 102.45)


def _on_island(lat, lng):
    return _ISL[0] <= lat <= _ISL[1] and _ISL[2] <= lng <= _ISL[3]


def _thai_person(name):
    name = (name or "").strip()
    if not name or name.startswith("คุณ"):
        return name or "-"
    return f"คุณ{name}"


def _first_name(name):
    return (name or "").strip().split(" ")[0]


def _cutoff_hhmm():
    env = os.environ.get("POSITIONING_MORNING_CUTOFF", "")
    if env:
        return env
    try:
        from ferry_model import load_model
        return load_model().get("island_cutoff", "09:00")
    except Exception:
        return "09:00"


def _haversine_km(a, b):
    lat1, lng1, lat2, lng2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = (math.sin((lat2 - lat1) / 2) ** 2
         + math.cos(lat1) * math.cos(lat2) * math.sin((lng2 - lng1) / 2) ** 2)
    return 2 * 6371 * math.asin(math.sqrt(h))


# ─────────────────────────────────────────────────────────────
# Ferry math
# ─────────────────────────────────────────────────────────────

def _zone_point(zone):
    try:
        from pickup_matcher import _load_points
        return _load_points().get((zone or "").lower())
    except Exception:
        return None


def ferry_math(now_ict, pickup_dt, zone, side_required):
    """Feasibility for a wrong-side driver + human-readable Thai lines."""
    try:
        from ferry_model import load_model
        m = load_model()
    except Exception:
        m = {}
    sail = m.get("sailings", {})
    first_s, last_s = sail.get("first", "06:30"), sail.get("last", "18:30")
    crossing = int(m.get("crossing_min", 30))
    pre = int(m.get("pre_boarding_min", 45))

    # drive from the destination-side pier to the pickup zone
    pier_key = m.get("pier_island", "ao sapparot pier") if side_required == "island" \
        else m.get("pier_mainland", "ao thammachat pier")
    leg_min = 30
    zp, pp = _zone_point(zone), _zone_point(pier_key)
    if zp and pp:
        km = _haversine_km((zp["lat"], zp["lng"]), (pp["lat"], pp["lng"]))
        leg_min = int(km / 35 * 60) + 10  # crude 35 km/h + 10 min buffer

    # Verified timetable (14 Aug): 45-min pre-boarding (cash-only counter,
    # no pre-purchase) — a sailing is catchable only if at the pier 45 min
    # before departure. Weather caveat: non-IMPOSSIBLE wording must say
    # "ตามตารางปกติ" — the operator reserves weather changes.
    lh, lm = map(int, last_s.split(":"))
    last_dt = now_ict.replace(hour=lh, minute=lm, second=0, microsecond=0)
    lines = []
    if now_ict <= last_dt - timedelta(minutes=pre):
        mins_left = int((last_dt - timedelta(minutes=pre) - now_ict).total_seconds() // 60)
        cls = "FEASIBLE"
        lines.append(f"⛴ ยังข้ามคืนนี้ทันตามตารางปกติ — เที่ยวสุดท้าย {last_s} "
                     f"ต้องถึงท่าก่อน {(last_dt - timedelta(minutes=pre)).strftime('%H:%M')} "
                     f"(อีก {mins_left} นาที · ตั๋วเงินสดหน้าท่าเท่านั้น)")
    else:
        from ferry_model import next_departure
        dep = next_departure(now_ict, m) or (now_ict + timedelta(days=1)).replace(
            hour=6, minute=30, second=0, microsecond=0)
        arrival = dep + timedelta(minutes=crossing + leg_min)
        margin = int((pickup_dt - arrival).total_seconds() // 60)
        lines.append(f"⛴ เที่ยวสุดท้ายวันนี้ {last_s} — ไม่ทันแล้ว (ต้องถึงท่าก่อน {pre} นาที)")
        lines.append(f"⛴ เที่ยวแรกพรุ่งนี้ {dep.strftime('%H:%M')} (ต้องถึงท่า "
                     f"{(dep - timedelta(minutes=pre)).strftime('%H:%M')}) + ข้าม ~{crossing}น. "
                     f"+ ขับไปจุดรับ ~{leg_min}น. → ถึงประมาณ {arrival.strftime('%H:%M')}")
        if margin >= 15:
            cls = "AT-RISK"
            lines.append(f"⏱ รับลูกค้า {pickup_dt.strftime('%H:%M')} → เหลือ margin ~{margin} นาที "
                         f"ตามตารางปกติ (เฉียดฉิว — อากาศ/รอบเรืออาจเปลี่ยน)")
        else:
            cls = "IMPOSSIBLE"
            lines.append(f"⏱ รับลูกค้า {pickup_dt.strftime('%H:%M')} → ไม่ทัน (ขาด ~{-margin} นาที) — ต้องแก้คืนนี้")
    return cls, lines


# ─────────────────────────────────────────────────────────────
# Scan
# ─────────────────────────────────────────────────────────────

def _scan(for_date=None):
    """Tomorrow's in-scope bookings with zone side + driver GPS side."""
    from db import _get_pool
    pool = _get_pool()
    if pool is None:
        return []
    now = datetime.now(ICT)
    target = for_date or (now + timedelta(days=1)).strftime("%Y-%m-%d")
    ch, cm = map(int, _cutoff_hhmm().split(":"))
    out = []
    with pool.connection() as conn:
        rows = conn.execute(
            "SELECT booking_id, pickup_ts, driver_id, provider_id, "
            "payload->>'Name', payload->>'Pickup_Location', "
            "payload->>'Pickup_Zone', payload->>'Dropoff_Location' "
            "FROM booking_cache WHERE tour_date = %s "
            "AND lower(coalesce(type_of_package,'')) = 'private transfer' "
            "AND pickup_ts IS NOT NULL AND provider_id IS NOT NULL "
            "ORDER BY pickup_ts LIMIT 100", (target,)).fetchall()
        ages = {r[0]: r for r in conn.execute(
            "SELECT driver_id, ts, lat, lng FROM driver_latest").fetchall()}
    from ferry_model import ISLAND_ZONES
    for (bid, pickup_ts, drv_code, provider_id, name,
         pickup_loc, zone, dropoff) in rows:
        p_ict = pickup_ts.astimezone(ICT)
        if (p_ict.hour, p_ict.minute) >= (ch, cm):
            continue
        z = (zone or "").strip().lower()
        if not z:
            try:
                from pickup_matcher import classify
                z = (classify(pickup_loc or "")[0] or "").lower()
            except Exception:
                z = ""
        side_required = "island" if z in ISLAND_ZONES else "mainland"
        drv_side, gps_age = "unknown", None
        r = ages.get((drv_code or "").upper()) if drv_code else None
        if r:
            gps_age = (datetime.now(timezone.utc) - r[1]).total_seconds()
            if gps_age <= GPS_MAX_AGE_S:
                drv_side = "island" if _on_island(r[2], r[3]) else "mainland"
        out.append({
            "booking_id": str(bid), "pickup_dt": p_ict, "zone": z,
            "side_required": side_required, "driver_code": drv_code,
            "provider_id": provider_id, "customer": (name or "-").strip(),
            "pickup_location": (pickup_loc or "-").strip()[:45],
            "dropoff": (dropoff or "-").strip()[:45],
            "driver_side": drv_side, "gps_age_s": gps_age,
            "check_date": target,
        })
    return out


def _provider_info(provider_id):
    try:
        from gps_ingest import provider_entry_for_id
        return provider_entry_for_id(provider_id) or {}
    except Exception:
        return {}


# ─────────────────────────────────────────────────────────────
# critical_alerts integration — wrong-side RED states
# ─────────────────────────────────────────────────────────────

def wrong_side_states():
    """States for the critical-alerts cron: repeats/ack/re-escalation come
    from that machinery. Emitted only in the evening window; a state
    vanishes (row auto-clears) when the driver's fresh GPS flips to the
    correct side or the evening window ends."""
    now = datetime.now(ICT)
    w0 = tuple(map(int, STATE_WINDOW[0].split(":")))
    w1 = tuple(map(int, STATE_WINDOW[1].split(":")))
    if not (w0 <= (now.hour, now.minute) <= w1):
        return {}
    states = {}
    try:
        from db import _get_pool
        pool = _get_pool()
        if pool is None:
            return {}
        target = (now + timedelta(days=1)).strftime("%Y-%m-%d")
        with pool.connection() as conn:
            rows = conn.execute(
                "SELECT booking_id, status FROM positioning_checks "
                "WHERE check_date = %s AND status IN ('wrong_gps','confirmed_wrong')",
                (target,)).fetchall()
        flagged = {r[0]: r[1] for r in rows}
        if not flagged:
            return {}
        for b in _scan(for_date=target):
            bid = b["booking_id"]
            if bid not in flagged:
                continue
            # self-clear: fresh GPS now shows the correct side
            if b["driver_side"] == b["side_required"]:
                _update_row(bid, target, status="ok_gps",
                            detail="GPS flipped to correct side")
                continue
            prov = _provider_info(b["provider_id"])
            cls, lines = ferry_math(now, b["pickup_dt"], b["zone"], b["side_required"])
            src = ("GPS" if flagged[bid] == "wrong_gps" else "คนขับยืนยันเอง")
            states[f"position:{bid}"] = {
                "alert_type": "positioning-wrong-side",
                "booking_id": bid,
                "customer": b["customer"],
                "pickup_time": b["pickup_dt"].strftime("%H:%M"),
                "route": f"{b['pickup_location']} → {b['dropoff']}",
                "driver_code": b["driver_code"],
                "provider_id": b["provider_id"],
                "extra_lines": [f"หลักฐาน: {src} · งานพรุ่งนี้เช้าฝั่ง"
                                f"{'เกาะ' if b['side_required'] == 'island' else 'แผ่นดินใหญ่'}"]
                               + lines + [f"ระดับ: {cls}"],
            }
    except Exception as e:
        logger.warning(f"[POS-LIVE] wrong_side_states failed: {e}")
    return states


# ─────────────────────────────────────────────────────────────
# DB row helpers
# ─────────────────────────────────────────────────────────────

def _upsert_row(b, status, driver_uid=None, detail=None, asked=False):
    from db import _get_pool
    pool = _get_pool()
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO positioning_checks (booking_id, check_date, side_required, "
            "zone, driver_id, provider_id, driver_uid, status, detail, asked_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s, CASE WHEN %s THEN now() END) "
            "ON CONFLICT (booking_id, check_date) DO UPDATE SET "
            "status = EXCLUDED.status, detail = EXCLUDED.detail, "
            "driver_uid = COALESCE(EXCLUDED.driver_uid, positioning_checks.driver_uid), "
            "asked_at = COALESCE(positioning_checks.asked_at, EXCLUDED.asked_at), "
            "updated_at = now()",
            (b["booking_id"], b["check_date"], b["side_required"], b["zone"],
             b["driver_code"], b["provider_id"], driver_uid, status, detail, asked))


def _update_row(booking_id, check_date, status=None, answer=None, detail=None):
    from db import _get_pool
    pool = _get_pool()
    with pool.connection() as conn:
        conn.execute(
            "UPDATE positioning_checks SET "
            "status = COALESCE(%s, status), "
            "answer = COALESCE(%s, answer), "
            "answered_at = CASE WHEN %s IS NOT NULL THEN now() ELSE answered_at END, "
            "detail = COALESCE(%s, detail), updated_at = now() "
            "WHERE booking_id = %s AND check_date = %s",
            (status, answer, answer, detail, booking_id, check_date))


def _get_row(booking_id, check_date):
    from db import _get_pool
    pool = _get_pool()
    with pool.connection() as conn:
        r = conn.execute(
            "SELECT status, driver_uid, side_required, asked_at FROM positioning_checks "
            "WHERE booking_id = %s AND check_date = %s",
            (booking_id, check_date)).fetchone()
    return r


# ─────────────────────────────────────────────────────────────
# LINE sends
# ─────────────────────────────────────────────────────────────

def _push(to, messages):
    try:
        resp = requests.post(
            "https://api.line.me/v2/bot/message/push",
            headers={"Authorization": f"Bearer {TRANSFER_LINE_TOKEN}",
                     "Content-Type": "application/json"},
            json={"to": to, "messages": messages}, timeout=10)
        if resp.status_code != 200:
            logger.warning(f"[POS-LIVE] push {to[:8]}…: {resp.status_code} {resp.text[:120]}")
        return resp.status_code == 200
    except Exception as e:
        logger.error(f"[POS-LIVE] push error: {e}")
        return False


def _ask_driver_card(b, prov):
    first = _thai_person(_first_name(prov.get("name") or ""))
    island_job = b["side_required"] == "island"
    where = "เกาะช้าง" if island_job else "ฝั่งแผ่นดินใหญ่ (ตราด)"
    correct_lbl = "อยู่บนเกาะแล้ว" if island_job else "อยู่ฝั่งแผ่นดินใหญ่แล้ว"
    wrong_lbl = "ยังไม่ได้ข้ามมา" if island_job else "ยังอยู่บนเกาะ"
    data = f"action=pos_confirm&key={b['booking_id']}&date={b['check_date']}"
    return {
        "type": "flex",
        "altText": f"ทีมงานแจ้งงานพรุ่งนี้ {b['pickup_dt'].strftime('%H:%M')} + ขอยืนยันตำแหน่งคืนนี้",
        "contents": {"type": "bubble",
            "body": {"type": "box", "layout": "vertical", "spacing": "sm", "contents": [
                {"type": "text", "text": f"สวัสดีค่ะ {first} 🙏", "weight": "bold",
                 "size": "md", "wrap": True},
                {"type": "text", "wrap": True, "size": "sm",
                 "text": (f"งานพรุ่งนี้เช้าของคุณ:\n"
                          f"⏰ {b['pickup_dt'].strftime('%H:%M')} · 👤 {b['customer']}\n"
                          f"📍 {b['pickup_location']} ({where})")},
                {"type": "text", "wrap": True, "size": "sm", "margin": "md",
                 "text": ("ทีมงานรบกวนเปิดแอป GPS (Traccar — สวิตช์เขียว) ตอนนี้เลยนะคะ "
                          "เพื่อให้ทีมยืนยันได้ว่าพร้อมสำหรับงานเช้า "
                          "หรือกดปุ่มยืนยันตำแหน่งด้านล่างแทนก็ได้ค่ะ")},
                {"type": "text", "wrap": True, "size": "xs", "color": "#999999", "margin": "md",
                 "text": ("ตั้งค่าไม่ให้เครื่องปิดแอปเอง: Samsung — เอา Traccar ออกจาก "
                          "Sleeping apps · Xiaomi/Redmi — เปิด Autostart · OPPO/vivo — "
                          "ตั้งแบตเป็น 'ไม่จำกัด' ให้แอปค่ะ")},
            ]},
            "footer": {"type": "box", "layout": "vertical", "spacing": "sm", "contents": [
                {"type": "button", "style": "primary", "color": "#06C755",
                 "action": {"type": "postback", "label": correct_lbl,
                            "data": data + "&ans=correct", "displayText": correct_lbl}},
                {"type": "button", "style": "secondary",
                 "action": {"type": "postback", "label": wrong_lbl,
                            "data": data + "&ans=wrong", "displayText": wrong_lbl}},
            ]}}}


def _team_amber(b, prov, reason):
    phone = prov.get("phone") or "-"
    txt = (f"🟠 ยังยืนยันตำแหน่งคืนนี้ไม่ได้ — งานพรุ่งนี้เช้า\n"
           f"ลูกค้า: {b['customer']} · Pickup {b['pickup_dt'].strftime('%H:%M')}\n"
           f"เส้นทาง: {b['pickup_location']} → {b['dropoff']}\n"
           f"คนขับ: {_thai_person(prov.get('name'))} · 📞 {phone}\n"
           f"เหตุผล: {reason}\n"
           f"รบกวนทีมโทรเช็คคืนนี้ค่ะ")
    return _push(TEAM_LINE_GROUP_ID, [{"type": "text", "text": txt}])


# ─────────────────────────────────────────────────────────────
# Cron
# ─────────────────────────────────────────────────────────────

@positioning_live_bp.route("/cron/positioning-evening", methods=["GET", "POST"])
def cron_positioning_evening():
    """~20:00 ask pass + ~21:30 follow-up (phase by ICT clock, or ?phase=).
    ?dry_run=true — no sends/writes. ?for_date=YYYY-MM-DD — scan that
    pickup date instead of tomorrow (testing). ?preview_to=<UID> — send
    the driver-ask card to that UID instead, no state writes."""
    dry_run = request.args.get("dry_run") == "true"
    for_date = request.args.get("for_date")
    preview_to = (request.args.get("preview_to") or "").strip() or None
    now = datetime.now(ICT)
    fh, fm = map(int, FOLLOWUP_AFTER.split(":"))
    phase = request.args.get("phase") or (
        "night_final" if (now.hour, now.minute) >= (22, 45)
        else "followup" if (now.hour, now.minute) >= (fh, fm) else "ask")
    actions = []

    # (c) 23:00 final escalation: an un-acked evening positioning card
    # means NO HUMAN KNOWS YET — one mention-all before the rung sleeps.
    # Acked cards sleep silently; the morning infeasible rung takes over.
    if phase == "night_final":
        try:
            from db import _get_pool
            pool = _get_pool()
            with pool.connection() as conn:
                rows = conn.execute(
                    "SELECT alert_key, booking_id FROM critical_alerts "
                    "WHERE cleared_at IS NULL AND acked_at IS NULL "
                    "AND alert_key LIKE 'position:%%'").fetchall()
            for k, bid in rows:
                if dry_run:
                    actions.append({"key": k, "action": "would_night_final"})
                    continue
                msgs = [
                    {"type": "textV2",
                     "text": ("{everyone} ‼️ ยังไม่มีใครกดรับทราบ — ปัญหาตำแหน่งคนขับ"
                              f"ของงานเช้าพรุ่งนี้ยังค้างอยู่ (Booking {bid})\n"
                              "ระบบจะหยุดเตือนคืนนี้ และเตือนต่ออัตโนมัติตอนเช้า — "
                              "รบกวนทีมดูก่อนนอนค่ะ"),
                     "substitution": {"everyone": {"type": "mention",
                                                   "mentionee": {"type": "all"}}}},
                ]
                if not _push(TEAM_LINE_GROUP_ID, msgs):
                    _push(TEAM_LINE_GROUP_ID, [{"type": "text",
                          "text": msgs[0]["text"].replace("{everyone} ", "")}])
                actions.append({"key": k, "action": "night_final_sent"})
        except Exception as e:
            logger.warning(f"[POS-LIVE] night_final failed: {e}")
        return jsonify({"ok": True, "phase": phase, "dry_run": dry_run,
                        "actions": actions}), 200
    try:
        bookings = _scan(for_date=for_date)
        for b in bookings:
            bid = b["booking_id"]
            prov = _provider_info(b["provider_id"])
            row = None if (dry_run or preview_to) else _get_row(bid, b["check_date"])
            status = row[0] if row else None

            if b["driver_side"] == b["side_required"]:
                actions.append({"booking": bid, "action": "ok_gps",
                                "age_min": int((b["gps_age_s"] or 0) // 60)})
                if not (dry_run or preview_to):
                    _upsert_row(b, "ok_gps", detail=f"GPS correct side, age {int((b['gps_age_s'] or 0)//60)}m")
                continue

            if b["driver_side"] != "unknown":
                actions.append({"booking": bid, "action": "wrong_side_red"})
                if not (dry_run or preview_to):
                    _upsert_row(b, "wrong_gps", detail=f"GPS shows {b['driver_side']}")
                continue  # red card sent by the critical pass triggered below

            # unknown position
            if phase == "ask":
                uid = (prov.get("line_user_id") or "").strip()
                if preview_to:
                    ok = _push(preview_to, [_ask_driver_card(b, prov)])
                    actions.append({"booking": bid, "action": "preview_ask_sent", "ok": ok})
                    continue
                if status in ("asked", "confirmed_ok", "confirmed_wrong", "no_reply"):
                    actions.append({"booking": bid, "action": f"already_{status}"})
                    continue
                if dry_run:
                    actions.append({"booking": bid, "action": "would_ask",
                                    "uid_known": uid.startswith("U")})
                    continue
                if not uid.startswith("U"):
                    _upsert_row(b, "unreachable", detail="no LINE UID")
                    _team_amber(b, prov, "คนขับไม่มี LINE ที่เชื่อมกับระบบ — ถาม GPS/ตำแหน่งเองไม่ได้")
                    actions.append({"booking": bid, "action": "unreachable_amber"})
                    continue
                if _push(uid, [_ask_driver_card(b, prov)]):
                    _upsert_row(b, "asked", driver_uid=uid, asked=True)
                    actions.append({"booking": bid, "action": "asked"})
                else:
                    _upsert_row(b, "unreachable", detail="LINE push failed")
                    _team_amber(b, prov, "ส่ง LINE หาคนขับไม่สำเร็จ")
                    actions.append({"booking": bid, "action": "push_failed_amber"})
            else:  # followup
                if dry_run or preview_to:
                    actions.append({"booking": bid, "action": "would_followup",
                                    "current_status": status})
                    continue
                if status == "asked":
                    _update_row(bid, b["check_date"], status="no_reply")
                    _team_amber(b, prov, "ถามคนขับทาง LINE แล้ว ยังไม่ตอบ (ถามเมื่อ ~20:00)")
                    actions.append({"booking": bid, "action": "no_reply_amber"})
                else:
                    actions.append({"booking": bid, "action": f"followup_skip_{status}"})

        # wrong-side reds: trigger an immediate critical pass (repeats/ack
        # live there) instead of waiting up to 10 min for the schedule
        if not (dry_run or preview_to) and any(a["action"] == "wrong_side_red" for a in actions):
            try:
                requests.get(
                    "https://thailand-tour-daily-report.onrender.com/cron/critical-alerts",
                    params={"key": os.environ.get("CRON_SECRET", "")}, timeout=25)
                actions.append({"action": "critical_pass_triggered"})
            except Exception as e:
                logger.warning(f"[POS-LIVE] immediate critical pass failed: {e}")

        return jsonify({"ok": True, "phase": phase, "dry_run": dry_run,
                        "scanned": len(bookings), "actions": actions}), 200
    except Exception as e:
        logger.error(f"[POS-LIVE] cron error: {e}")
        return jsonify({"ok": False, "reason": str(e)[:200]}), 500


# ─────────────────────────────────────────────────────────────
# Driver answer (relayed by transfer-line-webhook)
# ─────────────────────────────────────────────────────────────

@positioning_live_bp.route("/positioning/answer", methods=["POST"])
def positioning_answer():
    cron_secret = os.environ.get("CRON_SECRET", "")
    if cron_secret and request.args.get("key", "") != cron_secret:
        return jsonify({"error": "unauthorized"}), 401
    body = request.get_json(silent=True) or {}
    bid = (body.get("booking_id") or "").strip()
    check_date = (body.get("check_date") or "").strip()
    answer = (body.get("answer") or "").strip()  # correct | wrong
    by_uid = (body.get("by_uid") or "").strip()
    if not bid or answer not in ("correct", "wrong"):
        return jsonify({"status": "error", "message": "bad payload"}), 400
    try:
        row = _get_row(bid, check_date)
        if row is None:
            return jsonify({"status": "unknown"}), 200
        status, driver_uid, side_required, asked_at = row
        if driver_uid and by_uid and by_uid != driver_uid:
            logger.warning(f"[POS-LIVE] answer for {bid} from wrong UID {by_uid[:10]}")
            return jsonify({"status": "wrong_person"}), 200
        if status in ("confirmed_ok", "confirmed_wrong"):
            return jsonify({"status": "already", "answer": status}), 200
        if answer == "correct":
            _update_row(bid, check_date, status="confirmed_ok", answer="correct",
                        detail="driver confirmed correct side by tap")
            logger.info(f"[POS-LIVE] {bid}: driver confirmed CORRECT side")
            return jsonify({"status": "recorded_ok"}), 200
        _update_row(bid, check_date, status="confirmed_wrong", answer="wrong",
                    detail="driver confirmed wrong side by tap")
        logger.warning(f"[POS-LIVE] {bid}: driver confirmed WRONG side — escalating")
        try:
            requests.get(
                "https://thailand-tour-daily-report.onrender.com/cron/critical-alerts",
                params={"key": cron_secret}, timeout=25)
        except Exception as e:
            logger.warning(f"[POS-LIVE] escalation pass failed: {e}")
        return jsonify({"status": "recorded_wrong"}), 200
    except Exception as e:
        logger.error(f"[POS-LIVE] answer error: {e}")
        return jsonify({"status": "error", "message": str(e)[:150]}), 500
