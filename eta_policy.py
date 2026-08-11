"""
eta_policy.py — Stage 2 Step 6: precision-aware claim limits
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
The single source of truth for WHAT the system may claim about a job,
by pickup-pin precision tier (spec §3 Step 6, Orathai decision 1, 9 Aug):

  exact    -> minute-level ETA ("arrives 09:42")
  zone     -> ±10 min band, phrased "at risk"/"on track" — never minutes
  generic  -> coarse only: moving/not-moving, side-of-water, no-signal —
              NEVER any minute-level claim
  'upgrade-pending' geocode flag inherits its pin's zone tier;
  unknown/missing precision falls to coarse (the safe floor).

D7 (locked 1200 s): a position older than STALE_S is NO-SIGNAL, never
"stationary" — parked drivers with distance-filtered heartbeats must not
false-alarm. Every later engine (alerts Step 7, dashboard ETA display at
the Step-7/8 gates) consults this module instead of re-deciding policy.

Pure policy + one read-only debug route for the gate's table test.
No DB, no Zoho, no messages. Importing this changes no behaviour.
"""

from flask import Blueprint, jsonify

eta_policy_bp = Blueprint("eta_policy", __name__)

# Claim classes, weakest to strongest
COARSE = "coarse"
BAND = "band"
MINUTE = "minute"

_RANK = {COARSE: 0, BAND: 1, MINUTE: 2}

# Strongest claim each precision tier permits
_CLAIM_BY_TIER = {"exact": MINUTE, "zone": BAND, "generic": COARSE}

ZONE_BAND_MIN = 10   # ±band width for zone-tier ETA phrasing [spec proposed]
STALE_S = 1200       # D7: beyond this, the driver is no-signal, full stop

# Every rule/claim the later engines will make, mapped to the claim class
# it NEEDS. clamp() below degrades it to what the tier permits.
RULE_CLAIMS = {
    "eta-minute":      MINUTE,  # dashboard minute-level ETA display
    "eta-band":        BAND,    # dashboard ±10 min band display
    "at-risk":         BAND,    # alert rule 3 (stage-2 corrected ETA)
    "not-departed":    BAND,    # alert rule 2 (stage-1/2 math)
    "wrong-direction": COARSE,  # alert rule 4 (bearing divergence)
    "moving-status":   COARSE,  # dashboard coarse status
    "side-of-water":   COARSE,  # positioning checks (P-D)
    "no-signal":       COARSE,  # alert rule 1 — allowed at every tier
}


def claim_class(precision):
    """Strongest claim allowed for a pin's precision tier.
    Unknown/None -> coarse (safe floor)."""
    return _CLAIM_BY_TIER.get((precision or "").strip().lower(), COARSE)


def allowed(precision, claim):
    """May a claim of this class be made for this precision tier?"""
    return _RANK[claim] <= _RANK[claim_class(precision)]


def clamp(precision, claim):
    """The claim class actually permitted: the requested one if allowed,
    else degraded to the tier's ceiling. Rules call this instead of
    deciding for themselves."""
    limit = claim_class(precision)
    return claim if _RANK[claim] <= _RANK[limit] else limit


def no_signal(age_s):
    """D7: True when the position is too old to treat as current.
    None (never seen) is no-signal by definition."""
    return age_s is None or age_s > STALE_S


def policy_table():
    """The full (precision x rule) matrix — the Step-6 gate artifact.
    Cell = the claim class the rule may actually make at that tier."""
    tiers = ["exact", "zone", "generic", "upgrade-pending-zone", None]
    out = {}
    for t in tiers:
        # 'upgrade-pending' is a geocode flag, not a precision tier: it
        # inherits the pin's own tier (modelled here as zone, its floor
        # in practice). None models a booking with no precision at all.
        eff = "zone" if t == "upgrade-pending-zone" else t
        out[str(t)] = {rule: clamp(eff, need)
                       for rule, need in RULE_CLAIMS.items()}
    return out


@eta_policy_bp.route("/debug/eta-policy", methods=["GET"])
def debug_eta_policy():
    """Read-only gate surface: the exact matrix the engines will obey.
    Static policy, no data — safe to expose."""
    return jsonify({
        "ok": True,
        "constants": {"zone_band_min": ZONE_BAND_MIN, "stale_s": STALE_S},
        "table": policy_table(),
    }), 200
