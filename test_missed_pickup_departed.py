"""Missed-pickup false alarm (Orathai, 17 Nov 2026): a driver with fresh GPS
who passed near the pickup pin and is now far from it has collected the
customer -> no red card. Run: python3 -m pytest test_missed_pickup_departed.py -q
"""
from datetime import datetime, timedelta, timezone
import pytest

AWA_PIN = (12.024668, 102.291151)        # geocoded pin (735 m off)
AWA_REAL = (12.03116, 102.29256)         # real gate
KLONG_SON = (12.14168, 102.28025)        # on the way to the pier


def _pool(monkeypatch, pts):
    class _Cur:
        def __init__(self, d): self._d = d
        def fetchall(self): return self._d
    class _Conn:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, sql, *p): return _Cur(pts if "driver_positions" in sql else [])
    class _Pool:
        def connection(self): return _Conn()
    import db
    monkeypatch.setattr(db, "_get_pool", lambda: _Pool())


def test_departed_true_when_passed_pin_and_now_far(monkeypatch):
    from critical_alerts import _departed_after_pickup
    _pool(monkeypatch, [AWA_REAL, (12.06, 102.30), KLONG_SON])
    pickup = datetime.now(timezone.utc) - timedelta(minutes=30)
    assert _departed_after_pickup("P2091", pickup, *AWA_PIN, 60) is True


def test_not_departed_when_never_near_pin(monkeypatch):
    from critical_alerts import _departed_after_pickup
    _pool(monkeypatch, [(12.20, 102.30), KLONG_SON])
    pickup = datetime.now(timezone.utc) - timedelta(minutes=30)
    assert _departed_after_pickup("P2091", pickup, *AWA_PIN, 60) is False


def test_not_departed_when_still_at_pin(monkeypatch):
    from critical_alerts import _departed_after_pickup
    _pool(monkeypatch, [AWA_REAL, AWA_REAL])
    pickup = datetime.now(timezone.utc) - timedelta(minutes=30)
    assert _departed_after_pickup("P2091", pickup, *AWA_PIN, 60) is False


def test_not_departed_when_gps_stale(monkeypatch):
    from critical_alerts import _departed_after_pickup, SILENT_AFTER_S
    _pool(monkeypatch, [AWA_REAL, KLONG_SON])
    pickup = datetime.now(timezone.utc) - timedelta(minutes=30)
    assert _departed_after_pickup("P2091", pickup, *AWA_PIN, SILENT_AFTER_S + 1) is False


def test_arrival_radius_widened():
    import eta_checkpoints as ec
    assert ec.ARRIVAL_RADIUS_M == 1000
    # the real Awa gate vs the geocoded pin must now count as arrived
    assert ec._haversine_m(*AWA_REAL, *AWA_PIN) <= ec.ARRIVAL_RADIUS_M
