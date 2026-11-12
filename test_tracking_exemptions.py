"""Tracking exemptions (Orathai, 12 Nov 2026): GPS / roll-call / board alarms
only for Private Transfer jobs run by OUR OWN drivers. Join Transfers (KCE,
SWB) and outsourced partners (Garfield, Zoho Outsourced_Agent=true) are
exempt everywhere. Run:  python3 -m pytest test_tracking_exemptions.py -q
"""
import pytest

GARFIELD = "464930000004312043"
OWN_DRIVER = "464930000010310009"   # จตุพร P2091
KCE = "464930000004312015"

_REG = {
    GARFIELD:   {"id": GARFIELD, "code": "P000", "name": "Garfield Transfer",
                 "outsourced": True, "line_user_id": "Ugarfield", "phone": "0"},
    OWN_DRIVER: {"id": OWN_DRIVER, "code": "P2091", "name": "จตุพร",
                 "outsourced": False, "line_user_id": "Uown", "phone": "0"},
}


@pytest.fixture
def registry(monkeypatch):
    import gps_ingest
    monkeypatch.setattr(gps_ingest, "provider_entry_for_id",
                        lambda pid: _REG.get(str(pid)))
    yield


# ── provider_guard ────────────────────────────────────────────

def test_guard_blocks_outsourced(registry):
    from provider_guard import should_block, is_outsourced_provider
    assert is_outsourced_provider(GARFIELD) == (True, "Garfield Transfer")
    blocked, why = should_block(provider_id=GARFIELD)
    assert blocked and "Outsourced" in why


def test_guard_allows_own_driver(registry):
    from provider_guard import should_block, is_outsourced_provider
    assert is_outsourced_provider(OWN_DRIVER) == (False, "")
    assert should_block(provider_id=OWN_DRIVER,
                        booking={"Type_of_Package": "Private Transfer"}) == (False, "")


def test_guard_still_blocks_protected_and_join(registry):
    from provider_guard import should_block
    assert should_block(provider_id=KCE)[0] is True
    assert should_block(provider_id=OWN_DRIVER,
                        booking={"Type_of_Package": "Join Transfer"})[0] is True


def test_guard_unknown_provider_not_blocked(registry):
    """A provider missing from the registry must fail OPEN (not exempt)."""
    from provider_guard import should_block
    assert should_block(provider_id="999")[0] is False


# ── positioning_live._scan ────────────────────────────────────

def test_roll_call_skips_outsourced(registry, monkeypatch):
    """Garfield's tomorrow-morning Private Transfer must not enter the
    evening roll-call at all; our own driver's must."""
    from datetime import datetime, timedelta, timezone
    import positioning_live as pl

    pickup = datetime.now(timezone.utc).replace(hour=0, minute=30, second=0, microsecond=0) + timedelta(days=1)
    rows = [
        ("B-GAR", pickup, "P000", GARFIELD, "Cust A", "Kai Bae", "kai bae", "Trat"),
        ("B-OWN", pickup, "P2091", OWN_DRIVER, "Cust B", "Kai Bae", "kai bae", "Trat"),
    ]

    class _Cur:
        def __init__(self, data): self._d = data
        def fetchall(self): return self._d

    class _Conn:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, sql, *params):
            return _Cur(rows if "FROM booking_cache" in sql else [])

    class _Pool:
        def connection(self): return _Conn()

    import db
    monkeypatch.setattr(db, "_get_pool", lambda: _Pool())
    monkeypatch.setattr(pl, "_cutoff_hhmm", lambda: "12:00")

    out = pl._scan(for_date=pickup.strftime("%Y-%m-%d"))
    ids = {b["booking_id"] for b in out}
    assert "B-OWN" in ids
    assert "B-GAR" not in ids


# ── dashboard payload flag ────────────────────────────────────

def test_board_payload_carries_outsourced_flag(registry):
    """The templates key off bk.outsourced; the payload must set it from
    the registry so Garfield rows render like partner rows."""
    import gps_ingest
    assert bool(gps_ingest.provider_entry_for_id(GARFIELD)["outsourced"]) is True
    assert bool(gps_ingest.provider_entry_for_id(OWN_DRIVER)["outsourced"]) is False
