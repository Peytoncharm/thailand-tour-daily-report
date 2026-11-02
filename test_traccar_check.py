"""
test_traccar_check.py — offline tests for the Traccar-first timeline.
No DB, no Zoho, no LINE: booking_cache / gps_ingest registry / positions
/ pushes are monkeypatched. Run:  python3 -m pytest test_traccar_check.py -q
"""
import os
import sys
from collections import deque
from datetime import datetime, timezone, timedelta

import pytest

os.environ.setdefault("GPS_INGEST_SECRET", "testsecret")
os.environ.setdefault("TRANSFER_LINE_TOKEN", "x")

sys.path.insert(0, os.path.dirname(__file__))

ICT = timezone(timedelta(hours=7))


# ─────────────────────────────────────────────────────────────
# fixtures
# ─────────────────────────────────────────────────────────────

def _booking(bid, pickup_ict, prov_id="PROV1", name="Marti", last="Roger",
             lat=13.7449, lng=100.5556, zone="bangkok"):
    return {
        "id": bid, "Status": "Confirmed", "Type_of_Package": "Private Transfer",
        "Name": name, "Last_Name": last,
        "Pickup_Date_Time": pickup_ict.strftime("%Y-%m-%dT%H:%M:%S+07:00"),
        "Pickup_Location": "Grand Business Inn 2/4, Sukhumvit 11",
        "Dropoff_Location": "The Stage-Koh Chang",
        "Pickup_Lat": lat, "Pickup_Lng": lng, "Pickup_Zone": zone,
        "Provider_List": {"id": prov_id, "name": "มงคล คุ้มครอง"},
    }


REGISTRY = {
    "PROV1": {"id": "PROV1", "name": "มงคล คุ้มครอง", "code": "P852",
              "line_user_id": "Uaaa", "phone": "0812345678",
              "tracker_installed": True, "outsourced": False},
    "PROV2": {"id": "PROV2", "name": "สมชาย ไม่มีแอป", "code": "P999",
              "line_user_id": "Ubbb", "phone": "-",
              "tracker_installed": False, "outsourced": False},
    "PROV3": {"id": "PROV3", "name": "Garfield", "code": "P777",
              "line_user_id": "Uccc", "phone": "-",
              "tracker_installed": True, "outsourced": True},
}


@pytest.fixture
def app_client(monkeypatch):
    import traccar_check as tc
    import booking_cache
    import gps_ingest
    import provider_guard

    state = {"bookings": [], "positions": {}, "pushes": [], "logged": set(),
             "now": datetime.now(ICT)}

    monkeypatch.setattr(booking_cache, "get_bookings_for_dates",
                        lambda dates, type_of_package=None: [
                            b for b in state["bookings"]
                            if b["Pickup_Date_Time"][:10] in dates])
    monkeypatch.setattr(gps_ingest, "provider_entry_for_id",
                        lambda pid: REGISTRY.get(pid))
    monkeypatch.setattr(provider_guard, "should_block",
                        lambda **kw: (False, ""))
    monkeypatch.setattr(tc, "_latest_positions", lambda: dict(state["positions"]))
    monkeypatch.setattr(tc, "_now_ict", lambda: state["now"])
    monkeypatch.setattr(tc, "_push", lambda to, text: state["pushes"].append((to, text)) or True)
    monkeypatch.setattr(tc, "_already",
                        lambda bid, atype: (str(bid), atype) in state["logged"])
    monkeypatch.setattr(tc, "_log",
                        lambda bid, code, atype, detail: state["logged"].add((str(bid), atype)))

    from flask import Flask
    app = Flask(__name__)
    app.register_blueprint(tc.traccar_bp)
    return app.test_client(), state


def _at(state, hhmm_offset_min, pickup):
    """Set 'now' so that pickup is `hhmm_offset_min` minutes away."""
    state["now"] = pickup - timedelta(minutes=hhmm_offset_min)


# ─────────────────────────────────────────────────────────────
# stage detection + cards
# ─────────────────────────────────────────────────────────────

def test_t6_card_silent_driver_nudges_and_alerts(app_client):
    client, st = app_client
    pickup = datetime.now(ICT).replace(hour=8, minute=0, second=0, microsecond=0) + timedelta(days=1)
    st["bookings"] = [_booking("B1", pickup)]
    _at(st, 360, pickup)                      # exactly T-6
    r = client.get("/cron/traccar-check").get_json()
    assert len(r["team"]) == 1 and r["team"][0]["stage"] == "t6"
    assert r["team"][0]["icon"] == "🔴"
    assert "ไม่มีสัญญาณเลย" in r["team"][0]["text"]
    assert len(r["nudges"]) == 1 and "อีก 6 ชม." in r["nudges"][0]["text"]
    assert "คุณมงคล" in r["nudges"][0]["text"]          # คุณ+first name
    # pushes: team card + driver nudge
    tos = [p[0] for p in st["pushes"]]
    assert "Uaaa" in tos and len(st["pushes"]) == 2


def test_t6_green_with_distance_verdict(app_client):
    client, st = app_client
    pickup = datetime.now(ICT).replace(hour=8, minute=0, second=0, microsecond=0) + timedelta(days=1)
    st["bookings"] = [_booking("B1", pickup)]
    _at(st, 360, pickup)
    now_utc = st["now"].astimezone(timezone.utc)
    # driver 20 km from pickup, pinged 2 min ago
    st["positions"] = {"P852": (now_utc - timedelta(minutes=2), 13.90, 100.60)}
    r = client.get("/cron/traccar-check?dry_run=true").get_json()
    card = r["team"][0]
    assert card["icon"] == "🟢"
    assert "ห่างจุดรับ" in card["text"] and "ถึงทัน" in card["text"]
    assert r["nudges"] == []
    assert st["pushes"] == []                  # dry run sends nothing


def test_t1_too_far_is_flagged(app_client):
    client, st = app_client
    pickup = datetime.now(ICT).replace(hour=8, minute=0, second=0, microsecond=0) + timedelta(days=1)
    st["bookings"] = [_booking("B1", pickup)]
    _at(st, 60, pickup)                       # T-1
    now_utc = st["now"].astimezone(timezone.utc)
    # driver in Trat (~300 km away) 1 h before a Bangkok pickup
    st["positions"] = {"P852": (now_utc - timedelta(minutes=1), 12.24, 102.51)}
    r = client.get("/cron/traccar-check?dry_run=true").get_json()
    card = r["team"][0]
    assert card["stage"] == "t1" and card["icon"] == "🚨"
    assert "ไปไม่ทัน" in card["text"]


def test_no_stage_between_windows(app_client):
    client, st = app_client
    pickup = datetime.now(ICT).replace(hour=8, minute=0, second=0, microsecond=0) + timedelta(days=1)
    st["bookings"] = [_booking("B1", pickup)]
    _at(st, 240, pickup)                      # T-4: no stage
    r = client.get("/cron/traccar-check?dry_run=true").get_json()
    assert r["team"] == [] and r["nudges"] == []


def test_dedup_second_run_same_stage(app_client):
    client, st = app_client
    pickup = datetime.now(ICT).replace(hour=8, minute=0, second=0, microsecond=0) + timedelta(days=1)
    st["bookings"] = [_booking("B1", pickup)]
    _at(st, 362, pickup)
    client.get("/cron/traccar-check")
    n = len(st["pushes"])
    _at(st, 355, pickup)                      # still inside the T-6 window
    r = client.get("/cron/traccar-check").get_json()
    assert r["deduped"] == 1 and len(st["pushes"]) == n


def test_non_app_and_outsourced_are_skipped(app_client):
    client, st = app_client
    pickup = datetime.now(ICT).replace(hour=8, minute=0, second=0, microsecond=0) + timedelta(days=1)
    st["bookings"] = [_booking("B2", pickup, prov_id="PROV2"),
                      _booking("B3", pickup, prov_id="PROV3")]
    _at(st, 360, pickup)
    r = client.get("/cron/traccar-check?dry_run=true").get_json()
    assert r["team"] == []
    whys = sorted(s["why"] for s in r["skipped"])
    assert whys == ["no tracker app", "outsourced agent"]


# ─────────────────────────────────────────────────────────────
# evening roll-call
# ─────────────────────────────────────────────────────────────

def test_evening_rollcall_lists_green_and_red(app_client):
    client, st = app_client
    tomorrow = datetime.now(ICT).replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    st["bookings"] = [_booking("B1", tomorrow.replace(hour=8)),
                      _booking("B4", tomorrow.replace(hour=10, minute=30), name="Smith", last="")]
    st["now"] = datetime.now(ICT).replace(hour=21, minute=0)
    now_utc = st["now"].astimezone(timezone.utc)
    st["positions"] = {"P852": (now_utc - timedelta(minutes=5), 13.74, 100.55)}
    r = client.get("/cron/traccar-check?stage=evening").get_json()
    assert len(r["team"]) == 1
    txt = r["team"][0]["text"]
    assert txt.startswith("📋 เช็ค Traccar งานพรุ่งนี้")
    assert "🟢 08:00 Marti Roger" in txt and "🟢 10:30 Smith" in txt
    assert r["nudges"] == []
    # both logged → second run is silent
    r2 = client.get("/cron/traccar-check?stage=evening").get_json()
    assert r2["team"] == [] and r2["deduped"] == 2


def test_evening_silent_driver_gets_nudge(app_client):
    client, st = app_client
    tomorrow = datetime.now(ICT).replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    st["bookings"] = [_booking("B1", tomorrow.replace(hour=8))]
    st["now"] = datetime.now(ICT).replace(hour=21, minute=0)
    r = client.get("/cron/traccar-check?stage=evening").get_json()
    assert "🔴 08:00 Marti Roger" in r["team"][0]["text"]
    assert len(r["nudges"]) == 1 and "2 ทุ่ม" not in r["nudges"][0]["text"]
    assert "เปิดค้างไว้จนส่งลูกค้าเสร็จ" in r["nudges"][0]["text"]
    assert any(p[0] == "Uaaa" for p in st["pushes"])


# ─────────────────────────────────────────────────────────────
# privacy window (gps_ingest)
# ─────────────────────────────────────────────────────────────

@pytest.fixture
def ingest_client(monkeypatch):
    import gps_ingest as gi
    gi.GPS_INGEST_SECRET = "testsecret"
    gi.GPS_PRIVACY_WINDOW = True
    gi.POSITIONS.clear(); gi._window_cache.clear(); gi.VERIFY_OPEN.clear()
    monkeypatch.setattr(gi, "_lookup_code",
                        lambda c: {"id": "PROV1", "name": "มงคล", "code": "P852"} if c == "P852" else None)
    monkeypatch.setattr(gi, "_writeback_worker", lambda *a, **k: None)
    state = {"until": None, "raise": False}

    def _win(code):
        if state["raise"]:
            raise RuntimeError("db down")
        return state["until"]
    monkeypatch.setattr(gi, "_job_window_until", _win)
    import threading
    monkeypatch.setattr(threading, "Thread",
                        lambda *a, **k: type("T", (), {"start": lambda self: None})())
    from flask import Flask
    app = Flask(__name__)
    app.register_blueprint(gi.gps_bp)
    return app.test_client(), gi, state


def _ping(client, code="P852"):
    return client.get(f"/gps/ingest/testsecret?id={code}&lat=13.7&lon=100.5&timestamp=0")


def test_ping_outside_window_is_dropped(ingest_client):
    client, gi, st = ingest_client
    st["until"] = None
    assert _ping(client).status_code == 200          # app must not retry
    assert "P852" not in gi.POSITIONS                 # but nothing stored


def test_ping_inside_window_is_stored(ingest_client):
    client, gi, st = ingest_client
    st["until"] = datetime.now(timezone.utc) + timedelta(hours=1)
    assert _ping(client).status_code == 200
    assert "P852" in gi.POSITIONS


def test_verify_page_opens_two_hour_exception(ingest_client, monkeypatch):
    client, gi, st = ingest_client
    st["until"] = None
    monkeypatch.setenv("CRON_SECRET", "")
    assert client.get("/gps/verify/P852").status_code == 200
    assert _ping(client).status_code == 200
    assert "P852" in gi.POSITIONS


def test_window_fails_open_when_db_down(ingest_client):
    client, gi, st = ingest_client
    st["raise"] = True
    _ping(client)
    assert "P852" in gi.POSITIONS


def test_window_off_switch(ingest_client):
    client, gi, st = ingest_client
    gi.GPS_PRIVACY_WINDOW = False
    st["until"] = None
    _ping(client)
    assert "P852" in gi.POSITIONS
