"""
provider_guard.py — Last-resort safety net for provider LINE messages
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
TWO-TIER POLICY for protected providers (decision by Orathai, 9 Aug 2026):
  ALLOWED  — operational booking notifications and evening-before reminders
             (e.g. the 20:00 n8n "Remind to Provider (Transfer)" workflow,
             which since 9 Aug 2026 also covers Join Transfer bookings).
  NEVER    — GPS tracking links, approach links, watchdog alerts. These are
             driver-behaviour tools; KCE/SWB are company bus services whose
             coordinators cannot act on them (false watchdogs → auto-
             rebroadcast → relationship damage).

This module guards the NEVER tier only. It prevents automated GPS/approach/
watchdog messages from being sent to:
  1. Protected providers (KCE, SWB) — company bus services, not individual drivers
  2. Any booking whose Type_of_Package is NOT 'Private Transfer'
Evening reminders are sent from n8n and intentionally do NOT pass through
this guard.

Usage:
    from provider_guard import should_block, alert_pa_blocked

    blocked, reason = should_block(
        provider_id=prov_id,
        line_user_id=line_id,
        booking=booking_dict,   # must contain Type_of_Package if available
    )
    if blocked:
        alert_pa_blocked(reason, booking_id=bid, provider_name=pname)
        return  # do NOT send
"""

import os
import logging
import requests

logger = logging.getLogger(__name__)

PA_LINE_TOKEN = os.environ.get("PA_LINE_TOKEN", "")
TEAM_LINE_GROUP_ID = os.environ.get("TEAM_LINE_GROUP_ID", "")

# ── Protected providers ───────────────────────────────────────
# Company bus services that must NEVER receive automated GPS
# tracking, approach links, or watchdog alerts. (Operational booking
# notifications and evening-before reminders ARE allowed — see the
# two-tier policy in the module docstring.)

PROTECTED_PROVIDERS = {
    "464930000004312015": {
        "name": "KCE (Koh Chang Express)",
        "line_id": "Uce16e30d43e7d81d7bf42979bd08fd67",
    },
    "464930000004312017": {
        "name": "SWB (\u0e2a\u0e38\u0e27\u0e23\u0e23\u0e13\u0e20\u0e39\u0e21\u0e34\u0e1a\u0e39\u0e23\u0e1e\u0e32)",
        "line_id": "C3086868e615b0030e8722f92c6f9de98",
    },
}

# Reverse lookup: LINE User ID → provider name
_PROTECTED_LINE_IDS = {
    info["line_id"]: info["name"]
    for info in PROTECTED_PROVIDERS.values()
    if info.get("line_id")
}


# ── Guard functions ───────────────────────────────────────────

def is_protected_provider(provider_id=None, line_user_id=None):
    """Check if provider is on the protected list.
    Returns (blocked, provider_name).
    """
    if provider_id and str(provider_id) in PROTECTED_PROVIDERS:
        return True, PROTECTED_PROVIDERS[str(provider_id)]["name"]
    if line_user_id and line_user_id in _PROTECTED_LINE_IDS:
        return True, _PROTECTED_LINE_IDS[line_user_id]
    return False, ""


def is_join_transfer(booking):
    """True if booking is a non-Private Transfer type (Join Transfer, etc.).
    Pass a dict with at least 'Type_of_Package' key.
    """
    if not booking:
        return False
    pkg = (booking.get("Type_of_Package") or "").strip()
    if not pkg:
        return False  # no package info — can't block, let upstream decide
    return pkg != "Private Transfer"


def should_block(provider_id=None, line_user_id=None, booking=None):
    """Main guard. Returns (blocked: bool, reason: str).
    Call before any automated LINE push to a provider.
    """
    # Check 1: Protected provider
    blocked, name = is_protected_provider(provider_id, line_user_id)
    if blocked:
        return True, f"Protected provider: {name}"

    # Check 2: Non-Private Transfer booking
    if booking and is_join_transfer(booking):
        pkg = (booking.get("Type_of_Package") or "").strip()
        return True, f"Non-Private Transfer: {pkg}"

    return False, ""


def alert_pa_blocked(reason, booking_id=None, provider_name=None):
    """Send alert to PA LINE group when a message is blocked.
    Best-effort — failures logged, never raised.
    """
    token = PA_LINE_TOKEN
    group_id = TEAM_LINE_GROUP_ID
    if not token or not group_id:
        logger.warning(
            "[GUARD] Cannot alert PA — PA_LINE_TOKEN or TEAM_LINE_GROUP_ID not set"
        )
        return

    parts = [
        "\U0001f6e1\ufe0f Provider Guard \u0e1a\u0e25\u0e47\u0e2d\u0e04\u0e02\u0e49\u0e2d\u0e04\u0e27\u0e32\u0e21\u0e2d\u0e31\u0e15\u0e42\u0e19\u0e21\u0e31\u0e15\u0e34",
    ]
    if provider_name:
        parts.append(f"\U0001f690 Provider: {provider_name}")
    if booking_id:
        parts.append(f"\U0001f4cb Booking: {booking_id}")
    parts.append(f"\u274c \u0e40\u0e2b\u0e15\u0e38\u0e1c\u0e25: {reason}")
    parts.append(
        "\u0e02\u0e49\u0e2d\u0e04\u0e27\u0e32\u0e21\u0e19\u0e35\u0e49\u0e16\u0e39\u0e01\u0e1a\u0e25\u0e47\u0e2d\u0e04"
        "\u0e44\u0e21\u0e48\u0e44\u0e14\u0e49\u0e2a\u0e48\u0e07\u0e16\u0e36\u0e07 provider"
    )

    msg = "\n".join(parts)
    try:
        requests.post(
            "https://api.line.me/v2/bot/message/push",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json={"to": group_id, "messages": [{"type": "text", "text": msg}]},
            timeout=10,
        )
        logger.info(f"[GUARD] PA alerted: {reason}")
    except Exception as e:
        logger.error(f"[GUARD] PA alert failed: {e}")
