"""
traccar_check.py — Traccar-first pre-pickup timeline (Orathai, 2 Nov 2026)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Why: 1 Nov 2026, Marti Roger 08:00 — driver had the app (P852), got the
18:00 reminder, never turned it on; the first alert anyone saw was
"เลยเวลารับ" at 08:17. The evening positioning check only covers island
pickups before 09:00, so a Bangkok → Koh Chang job slipped through.

Scope: Private Transfer, Confirmed, assigned driver with
Tracker_Installed = true, valid LINE UID, not outsourced (Garfield etc.),
not a protected/join provider (provider_guard). Drivers WITHOUT the app
keep the old browser-link flow untouched.

Stages (ICT) — one cron every 15 min decides the stage from minutes to
pickup; the evening roll-call is a separate daily 21:00 call:

  evening  21:00 day before    roll-call of ALL tomorrow's app-driver jobs
                                🟢 signal seen / 🔴 silent (→ driver nudge)
  t6       pickup − 6 h        card per job: signal + distance/ETA verdict
  t3       pickup − 3 h        same, escalated
  t1       pickup − 1 h        same, 🚨

Silent = no stored position for the driver's code in the last N min
(60 at evening, 30 at t6/t3/t1). A silent driver gets ONE LINE nudge per
stage; the team gets ONE card per job per stage (alert_log dedup, types
traccar-team-<stage> / traccar-nudge-<stage>). Distance verdict comes
from feasibility.compute (ferry-aware ETA, speed tiers, 20-min GPS
freshness) — the same engine the dashboard uses, so they always agree.

Reads booking_cache + driver_latest (+ in-memory POSITIONS fallback).
Zero Zoho reads on the happy path. ?dry_run=true → JSON of what WOULD
be sent, nothing pushed, nothing logged. Auth: app-level /cron gate.

Not here (by design): auto-rebroadcast (phase 2), T-30 n8n branch,
tracker-first matching (phase 3).
"""

import logging
import os
import math
from datetime import datetime, timezone, timedelta

from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)

traccar_bp = Blueprint("traccar_check", __name__)

ICT = timezone(timedelta(hours=7))
TRANSFER_LINE_TOKEN = os.environ.get("TRANSFER_LINE_TOKEN", "")
TEAM_LINE_GROUP_ID = os.environ.get(
    "DRIVER_OPS_LINE_GROUP_ID", "Cde1194fd7767b00e6393055844ef45bd")

STAGE_MINUTES = {"t6": 360, "t3": 180, "t1": 60}
STAGE_HALF_WINDOW_MIN = 7.5            # cron every 15 min → each stage hit once
SILENT_MIN = {"evening": 60, "t6": 30, "t3": 30, "t1": 30}
EVENING_LABEL = "21:00"


# ─────────────────────────────────────────────────────────────
# Small helpers
# ─────────────────────────────────────────────────────────────

def _now_ict():
    return datetime.now(ICT)


def _thai_person(name):
    name = (name or "").strip()
    if not name or name.startswith("คุณ"):
        return name or "-"
    return f"คุณ{name}"


def _first(name):
    return (name or "").strip().split(" ")[0]


def _haversine_km(lat1, lng1, lat2, lng2):
    a, b, c, d = map(math.radians, (lat1, lng1, lat2, lng2))
    h = math.sin((c - a) / 2) ** 2 + math.cos(a) * math.cos(c) * math.sin((d - b) / 2) ** 2
    return 2 * 6371 * math.asin(math.sqrt(h))


def _push(to, text):
    """Text push via Transfer OA. True only on HTTP 200."""
    if not TRANSFER_LINE_TOKEN or not to:
        return False
    try:
        import requests
        r = requests.post(
            "https://api.line.me/v2/bot/message/push",
            headers={"Authorization": f"Bearer {TRANSFER_LINE_TOKEN}",
                     "Content-Type": "application/json"},
            json={"to": to, "messages": [{"type": "text", "text": text}]},
            timeout=10)
        if r.status_code != 200:
            logger.error(f"[TRACCAR-CHECK] push {to[:8]}…: {r.status_code} {r.text[:120]}")
        return r.status_code == 200
    except Exception as e:
        logger.error(f"[TRACCAR-CHECK] push error: {e}")
        return False


# ─────────────────────────────────────────────────────────────
# alert_log idempotency (one row per booking per type)
# ─────────────────────────────────────────────────────────────

def _already(booking_id, atype):
    try:
        from db import _get_pool
        pool = _get_pool()
        if pool is None:
            return False
        with pool.connection() as conn:
            row = conn.execute(
                "SELECT 1 FROM alert_log WHERE booking_id = %s AND alert_type = %s "
                "AND channel = 'live' LIMIT 1", (str(booking_id), atype)).fetchone()
        return row is not None
    except Exception as e:
        logger.warning(f"[TRACCAR-CHECK] dedup read failed ({atype}): {e}")
        return False


def _log(booking_id, driver_code, atype, detail):
    try:
        from db import _get_pool
        pool = _get_pool()
        if pool is None:
            return
        with pool.connection() as conn:
            conn.execute(
                "INSERT INTO alert_log (booking_id, driver_id, alert_type, channel, detail) "
                "VALUES (%s, %s, %s, 'live', %s)",
                (str(booking_id), driver_code, atype, (detail or "")[:400]))
    except Exception as e:
        logger.warning(f"[TRACCAR-CHECK] log write failed ({atype}): {e}")


# ─────────────────────────────────────────────────────────────
# Data
# ─────────────────────────────────────────────────────────────

def _latest_positions():
    """code(upper) → (ts_utc, lat, lng). DB first, memory fallback."""
    out = {}
    try:
        from db import _get_pool
        pool = _get_pool()
        if pool is not None:
            with pool.connection() as conn:
                for code, ts, lat, lng in conn.execute(
                        "SELECT driver_id, ts, lat, lng FROM driver_latest").fetchall():
                    out[(code or "").upper()] = (ts, lat, lng)
    except Exception as e:
        logger.warning(f"[TRACCAR-CHECK] driver_latest read failed: {e}")
    try:
        from gps_ingest import POSITIONS
        for code, entry in POSITIONS.items():
            pts = entry.get("points")
            if not pts:
                continue
            last = pts[-1]
            ts = entry["last_seen"]
            cur = out.get(code)
            if cur is None or ts > cur[0]:
                out[code] = (ts, last["lat"], last["lng"])
    except Exception as e:
        logger.warning(f"[TRACCAR-CHECK] memory positions read failed: {e}")
    return out


def _candidate_bookings(dates):
    """Cached bookings (raw Zoho-shaped) for the dates, Private Transfer +
    Confirmed + assigned, enriched with provider entry + eligibility."""
    from booking_cache import get_bookings_for_dates
    from gps_ingest import provider_entry_for_id
    from provider_guard import should_block

    rows = get_bookings_for_dates(dates, type_of_package="Private Transfer") or []
    out = []
    for r in rows:
        if (r.get("Status") or "").strip().lower() != "confirmed":
            continue
        prov = r.get("Provider_List")
        pid = prov.get("id") if isinstance(prov, dict) else None
        if not pid:
            continue
        pdt_raw = r.get("Pickup_Date_Time")
        try:
            pickup_dt = datetime.fromisoformat(pdt_raw.replace("Z", "+00:00")).astimezone(ICT)
        except Exception:
            continue
        entry = provider_entry_for_id(pid) or {}
        reason = None
        if not entry:
            reason = "provider not in registry"
        elif entry.get("outsourced"):
            reason = "outsourced agent"
        elif not entry.get("tracker_installed"):
            reason = "no tracker app"
        elif not entry.get("line_user_id"):
            reason = "no LINE UID"
        else:
            blocked, why = should_block(provider_id=pid, booking={
                "Type_of_Package": r.get("Type_of_Package")})
            if blocked:
                reason = f"guard: {why}"
        out.append({
            "booking_id": str(r.get("id")),
            "customer": ((r.get("Name") or "") + " " + (r.get("Last_Name") or "")).strip() or "-",
            "pickup_dt": pickup_dt,
            "pickup_location": (r.get("Pickup_Location") or "-").strip(),
            "dropoff": (r.get("Dropoff_Location") or "-").strip(),
            "pickup_lat": r.get("Pickup_Lat"),
            "pickup_lng": r.get("Pickup_Lng"),
            "pickup_zone": (r.get("Pickup_Zone") or "").strip().lower(),
            "provider_id": pid,
            "driver_name": entry.get("name") or (prov.get("name") if isinstance(prov, dict) else "") or "-",
            "driver_code": entry.get("code"),
            "driver_uid": entry.get("line_user_id"),
            "driver_phone": entry.get("phone") or "-",
            "skip_reason": reason,
        })
    out.sort(key=lambda b: b["pickup_dt"])
    return out


def _signal(b, positions, silent_min, now_utc, now_ict=None):
    """Attach signal + distance/verdict fields to booking b."""
    now_ict = now_ict or now_utc.astimezone(ICT)
    pos = positions.get((b["driver_code"] or "").upper())
    b["last_seen_ict"] = None
    b["age_min"] = None
    b["silent"] = True
    b["km"] = None
    b["verdict"] = "unknown"
    b["verdict_detail"] = ""
    if not pos:
        return b
    ts, lat, lng = pos
    age = (now_utc - ts).total_seconds()
    b["last_seen_ict"] = ts.astimezone(ICT).strftime("%H:%M")
    b["age_min"] = int(age // 60)
    b["silent"] = age > silent_min * 60
    if b["pickup_lat"] is not None and b["pickup_lng"] is not None:
        try:
            b["km"] = round(_haversine_km(lat, lng, float(b["pickup_lat"]),
                                          float(b["pickup_lng"])), 1)
        except Exception:
            pass
    try:
        from feasibility import compute
        f = compute(b["pickup_dt"],
                    float(b["pickup_lat"]) if b["pickup_lat"] is not None else None,
                    float(b["pickup_lng"]) if b["pickup_lng"] is not None else None,
                    b["pickup_zone"], lat, lng, age, now=now_ict)
        b["verdict"] = f.get("status", "unknown")
        b["eta"] = f.get("eta")
        b["slack_min"] = f.get("slack_min")
        b["verdict_detail"] = f.get("detail", "")
    except Exception as e:
        logger.warning(f"[TRACCAR-CHECK] feasibility failed {b['booking_id']}: {e}")
    return b


# ─────────────────────────────────────────────────────────────
# Message builders
# ─────────────────────────────────────────────────────────────

def _signal_line(b):
    if b["silent"]:
        if b["last_seen_ict"]:
            return f"📡 ไม่มีสัญญาณ {b['age_min']} นาที (ล่าสุด {b['last_seen_ict']})"
        return "📡 ไม่มีสัญญาณเลย"
    return f"📡 สัญญาณล่าสุด {b['last_seen_ict']} ({b['age_min']} นาที)"


def _distance_line(b):
    if b["silent"]:
        return None
    if b["km"] is None:
        return "📍 ระบุพิกัดจุดรับไม่ได้"
    v = b["verdict"]
    km = f"📍 ห่างจุดรับ {b['km']:.0f} กม."
    if v == "ok":
        return f"{km} · ถึงทัน ✅ (ETA {b.get('eta')})"
    if v == "at_risk":
        return f"{km} · ⚠️ เสี่ยงไปไม่ทัน (ETA {b.get('eta')})"
    if v == "infeasible":
        return f"{km} · 🚨 ไปไม่ทัน (ETA {b.get('eta')})"
    return km


def _icon(b):
    if b["silent"]:
        return "🔴"
    if b["verdict"] == "infeasible":
        return "🚨"
    if b["verdict"] == "at_risk":
        return "⚠️"
    return "🟢"


def _team_card(stage, b):
    head = {"t6": "T-6", "t3": "T-3", "t1": "T-1 🚨"}[stage]
    lines = [
        f"{_icon(b)} {head} · {b['pickup_dt'].strftime('%H:%M')} {b['customer']} · "
        f"{_thai_person(_first(b['driver_name']))} {b['driver_code']}",
        f"🛣 {b['pickup_location'][:40]} → {b['dropoff'][:40]}",
        _signal_line(b),
    ]
    d = _distance_line(b)
    if d:
        lines.append(d)
    if b["silent"]:
        if stage == "t1":
            lines.append(f"🚨 ต้องจัดการเดี๋ยวนี้ — โทร {b['driver_phone']}")
        elif stage == "t3":
            lines.append(f"🔴 ระบบเตือนคนขับแล้ว — โทรคนขับเดี๋ยวนี้ {b['driver_phone']}")
        else:
            lines.append("🔴 ระบบเตือนคนขับแล้ว — จะเช็คอีกที T-3")
    elif b["verdict"] in ("at_risk", "infeasible"):
        lines.append(f"⚠️ โทรคนขับเช็ค {b['driver_phone']}")
    return "\n".join(lines)


def _driver_nudge(stage, b):
    first = _thai_person(_first(b["driver_name"]))
    t = b["pickup_dt"].strftime("%H:%M")
    if stage == "evening":
        return (f"{first}คะ ยังไม่เห็นตำแหน่งจากแอปค่ะ 🙏\n"
                f"งานพรุ่งนี้ {t} รับ {b['customer']} ที่ {b['pickup_location'][:40]}\n"
                "รบกวนเปิดแอป Traccar → สวิตช์ \"ติดตามอย่างต่อเนื่อง\" ให้เขียว → "
                "กด \"ส่งสถานที่\" แล้วเปิดค้างไว้จนส่งลูกค้าเสร็จนะคะ 🔌 เสียบสายชาร์จในรถด้วยค่ะ")
    hrs = {"t6": "6", "t3": "3", "t1": "1"}[stage]
    extra = ""
    if stage == "t3":
        extra = "\nถ้าทีมงานยังไม่เห็นตำแหน่ง จะโทรหานะคะ"
    if stage == "t1":
        extra = "\nถ้าแอปมีปัญหา ตอบข้อความนี้บอกทีมงานด้วยค่ะ"
    return (f"{first}คะ อีก {hrs} ชม. ถึงเวลารับ {b['customer']} ({t}) "
            f"แอป Traccar ยังเปิดอยู่ไหมคะ ทีมงานยังไม่เห็นตำแหน่งค่ะ 🙏\n"
            "เช็คสวิตช์เขียว + กด \"ส่งสถานที่\" ให้หน่อยค่ะ" + extra)


def _rollcall(items):
    lines = [f"📋 เช็ค Traccar งานพรุ่งนี้ ({EVENING_LABEL})"]
    for b in items:
        who = f"{_thai_person(_first(b['driver_name']))} {b['driver_code']}"
        if b["silent"]:
            lines.append(f"🔴 {b['pickup_dt'].strftime('%H:%M')} {b['customer']} — {who} "
                         f"ยังไม่เห็น → ระบบเตือนคนขับแล้ว จะเช็คอีกที T-6")
        else:
            lines.append(f"🟢 {b['pickup_dt'].strftime('%H:%M')} {b['customer']} — {who} "
                         f"เห็นตำแหน่ง {b['last_seen_ict']}")
    n_red = sum(1 for b in items if b["silent"])
    if n_red:
        lines.append(f"\n🔴 {n_red} คน — ถ้าสะดวกโทรเช็คคืนนี้ได้เลยค่ะ")
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────
# Stage selection
# ─────────────────────────────────────────────────────────────

def _stage_for(b, now_ict):
    mins = (b["pickup_dt"] - now_ict).total_seconds() / 60
    for stage, target in STAGE_MINUTES.items():
        if abs(mins - target) <= STAGE_HALF_WINDOW_MIN:
            return stage
    return None


# ─────────────────────────────────────────────────────────────
# Cron
# ─────────────────────────────────────────────────────────────

@traccar_bp.route("/cron/traccar-check", methods=["GET", "POST"])
def cron_traccar_check():
    """?stage=evening (daily 21:00) or no stage (every 15 min → t6/t3/t1
    auto). ?dry_run=true → plan only. ?date=YYYY-MM-DD with stage=evening
    overrides the target day (testing)."""
    dry = (request.args.get("dry_run") or "").strip().lower() in ("true", "1", "yes")
    stage_req = (request.args.get("stage") or "").strip().lower()
    now_ict = _now_ict()
    now_utc = now_ict.astimezone(timezone.utc)

    if stage_req == "evening":
        target = request.args.get("date") or (now_ict + timedelta(days=1)).strftime("%Y-%m-%d")
        dates = [target]
    else:
        dates = [now_ict.strftime("%Y-%m-%d"), (now_ict + timedelta(days=1)).strftime("%Y-%m-%d")]

    bookings = _candidate_bookings(dates)
    positions = _latest_positions()
    plan = {"stage": stage_req or "auto", "now_ict": now_ict.strftime("%Y-%m-%d %H:%M"),
            "dry_run": dry, "team": [], "nudges": [], "skipped": [], "deduped": 0}

    def _skip(b, why):
        plan["skipped"].append({"booking_id": b["booking_id"], "pickup": b["pickup_dt"].strftime("%d %H:%M"),
                                "driver": b["driver_name"], "why": why})

    # ── Evening roll-call ───────────────────────────────────
    if stage_req == "evening":
        items = []
        for b in bookings:
            if b["skip_reason"]:
                _skip(b, b["skip_reason"]); continue
            if _already(b["booking_id"], "traccar-evening"):
                plan["deduped"] += 1; continue
            _signal(b, positions, SILENT_MIN["evening"], now_utc, now_ict)
            items.append(b)
        if items:
            text = _rollcall(items)
            plan["team"].append({"to": "group", "text": text})
            if not dry:
                if _push(TEAM_LINE_GROUP_ID, text):
                    for b in items:
                        _log(b["booking_id"], b["driver_code"], "traccar-evening",
                             "silent" if b["silent"] else f"ok {b['last_seen_ict']}")
            for b in items:
                if b["silent"]:
                    txt = _driver_nudge("evening", b)
                    plan["nudges"].append({"booking_id": b["booking_id"], "driver": b["driver_name"],
                                           "code": b["driver_code"], "text": txt})
                    if not dry and not _already(b["booking_id"], "traccar-nudge-evening"):
                        if _push(b["driver_uid"], txt):
                            _log(b["booking_id"], b["driver_code"], "traccar-nudge-evening", "sent")
        logger.info(f"[TRACCAR-CHECK] evening: {len(items)} jobs, "
                    f"{sum(1 for b in items if b['silent'])} silent, dry={dry}")
        return jsonify(plan), 200

    # ── T-6 / T-3 / T-1 ─────────────────────────────────────
    for b in bookings:
        stage = _stage_for(b, now_ict)
        if not stage:
            continue
        if b["skip_reason"]:
            _skip(b, b["skip_reason"]); continue
        if _already(b["booking_id"], f"traccar-team-{stage}"):
            plan["deduped"] += 1; continue
        _signal(b, positions, SILENT_MIN[stage], now_utc, now_ict)
        card = _team_card(stage, b)
        plan["team"].append({"to": "group", "stage": stage, "booking_id": b["booking_id"],
                             "icon": _icon(b), "text": card})
        if not dry:
            if _push(TEAM_LINE_GROUP_ID, card):
                _log(b["booking_id"], b["driver_code"], f"traccar-team-{stage}",
                     f"{_icon(b)} km={b['km']} verdict={b['verdict']}")
        if b["silent"]:
            txt = _driver_nudge(stage, b)
            plan["nudges"].append({"booking_id": b["booking_id"], "stage": stage,
                                   "driver": b["driver_name"], "code": b["driver_code"], "text": txt})
            if not dry and not _already(b["booking_id"], f"traccar-nudge-{stage}"):
                if _push(b["driver_uid"], txt):
                    _log(b["booking_id"], b["driver_code"], f"traccar-nudge-{stage}", "sent")

    logger.info(f"[TRACCAR-CHECK] auto: {len(plan['team'])} cards, "
                f"{len(plan['nudges'])} nudges, {len(plan['skipped'])} skipped, dry={dry}")
    return jsonify(plan), 200
