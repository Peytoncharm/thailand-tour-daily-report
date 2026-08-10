"""
pickup_matcher.py — Stage 1.3: free-text location → zone/coordinates
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Classifies Koh_Chang_Orders' free-text Pickup_Location / Dropoff_Location
into the validated coordinate table (pickup_points_draft.csv, shipped in
this repo — the same file db.py imports into Postgres at boot).

Offline-tested 9 Aug 2026 against all 807 historical bookings:
  pickup 89.0% matched, dropoff 59.7%, route derivable 50.8%.

Precision tiers (Orathai's decision, 9 Aug):
  exact   — airports / piers / terminals (a confirmed point)
  zone    — beach/town zone centroid
  generic — island-level "koh chang generic": COARSE. Valid for early
            checkpoints only; NEVER for minute-level ETA claims.
No Bangkok generic (decision 2): confidently-wrong ETAs are worse than a
miss — unmatched Bangkok strings await per-booking geocoding (later stage).

Pure functions, no I/O beyond loading the bundled CSV once. Never raises.
"""

import csv
import logging
import os
import re

logger = logging.getLogger(__name__)

_POINTS = None  # place_name -> {lat, lng, precision}


def _load_points():
    global _POINTS
    if _POINTS is not None:
        return _POINTS
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "pickup_points_draft.csv")
    pts = {}
    try:
        with open(path, encoding="utf-8-sig", newline="") as f:
            for r in csv.DictReader(f):
                name = (r.get("place_name") or "").strip().lower()
                if not name:
                    continue
                pts[name] = {
                    "lat": float(r["lat"]),
                    "lng": float(r["lng"]),
                    "precision": (r.get("precision") or "zone").strip().lower(),
                }
    except Exception as e:
        logger.error(f"[MATCHER] coordinate file load failed: {e}")
    _POINTS = pts
    return pts


# ── Tier 1: canonical zone names + spelling variants (decision 3) ──
ZONE_KEYWORDS = {
    "suvarnabhumi": ["suvarnabhumi", "suvannabhumi", "suvarnaphumi", "suvanaphumi",
                     "suvannaphumi", "bkk airport", "airport bkk", "bangkok airport",
                     "airport bangkok", "airport suvana", "airport suvanna", "สุวรรณภูมิ"],
    "don mueang": ["don mueang", "don mueng", "donmeung", "donmuang", "dmk"],
    "laem ngop pier": ["laem ngop", "lam ngop"],
    "ao sapparot pier": ["ao sapparot", "sapparot", "sapporot", "อ่าวสับปะรด"],
    "ao thammachat pier": ["thammachat", "ao thammachat"],
    "white sand beach": ["white sand", "kc grand", "kacha", "cookies hotel", "cookie hotel"],
    "klong prao": ["klong prao", "klong proa", "klong prow", "klong praow"],
    "kai bae": ["kai bae", "kaibae", "kae bae", "kea bae", "ka bae", "kaibea",
                "kai bea", "ไก่แบ้"],
    "lonely beach": ["lonely beach"],
    "bailan beach": ["bailan"],   # own zone since 9 Aug (row 24)
    "bang bao pier": ["bang bao", "bangbao", "bang boa"],
    "salak phet": ["salak phet", "salakphed", "salakphet"],
    "klong son": ["klong son", "คลองสน"],
    "pattaya": ["pattaya", "jomtien", "พัทยา"],
    "trat airport": ["trat airport", "trad airport", "trat air port"],  # before trat town
    "trat town": ["trat town", "trat bus", "trad bus", "trat station"],
    "chanthaburi": ["chanthaburi", "จันทบุรี"],
    "rayong": ["rayong"],
    "ban phe pier": ["ban phe", "ban pae", "banphe"],
    "mo chit bus terminal": ["mo chit", "mochit", "morchit", "หมอชิต"],
    "ekkamai bus terminal": ["ekkamai", "ekamai", "เอกมัย"],
    "krung thep aphiwat": ["krung thep aphiwat", "aphiwat", "bang sue",
                           "hua lamphong", "hua lam phong", "hua lampong"],
    "koh kood ao salad pier": ["ao salad"],
    "laem sok pier": ["laem sok", "lam sok", "lam sork", "laem sork"],
    "trat bus terminal": [],
}

# ── Tier 2: resort/landmark → zone. 'data' = evidenced by co-occurrence in
#    historical booking strings; 'geo' = assumed geography (reviewed by
#    Orathai as part of the step-5/6 gate before the switch turns on) ──
RESORT_ALIASES = {
    "awa": ("kai bae", "geo"), "sylvan": ("kai bae", "geo"), "sylvian": ("kai bae", "geo"),
    "garden resort": ("kai bae", "geo"), "gajapuri": ("kai bae", "data"),
    "gajaburi": ("kai bae", "data"), "kb resort": ("kai bae", "geo"),
    "k.b. resort": ("kai bae", "geo"), "k.b.resort": ("kai bae", "geo"),
    "mam ": ("kai bae", "data"), "mame ": ("kai bae", "data"),
    "sea escape": ("kai bae", "data"), "montra": ("kai bae", "data"),
    "peyton": ("kai bae", "data"), "office": ("kai bae", "data"),
    "the chill": ("kai bae", "geo"), "เดอะชิลล์": ("kai bae", "geo"),
    "cliff beach": ("kai bae", "geo"), "plaloma": ("kai bae", "geo"),
    "sanook sanang": ("kai bae", "geo"), "sannok": ("kai bae", "geo"),
    "snook sanang": ("kai bae", "geo"),
    "seabreeze": ("kai bae", "geo"), "sea breeze": ("kai bae", "geo"),
    "seebreaze": ("kai bae", "geo"),
    "coral resort": ("kai bae", "geo"),
    "porn": ("kai bae", "geo"), "porns": ("kai bae", "geo"),
    "cj kai": ("kai bae", "data"), "jop": ("kai bae", "data"),
    "kst": ("kai bae", "data"), "the green resort": ("kai bae", "data"),
    "green resort": ("kai bae", "data"),
    "dinso": ("klong prao", "geo"), "paradise bungalow": ("klong prao", "geo"),
    "paradise bugalow": ("klong prao", "geo"), "paradise resort": ("klong prao", "geo"),
    "paradise rom": ("klong prao", "geo"),
    "the dewa": ("klong prao", "geo"), "dewa": ("klong prao", "geo"),
    "centara": ("klong prao", "geo"), "flora": ("klong prao", "geo"),
    "barali": ("klong prao", "geo"), "aana": ("klong prao", "geo"),
    "the stage": ("klong prao", "geo"), "stage hotel": ("klong prao", "geo"),
    "hotel stage": ("klong prao", "geo"),
    # Orathai corrections 9 Aug: The Retreat is KAI BAE, Magic is KLONG PRAO
    "the retreat": ("kai bae", "orathai"),
    "retreat hotel": ("kai bae", "orathai"), "retreat resort": ("kai bae", "orathai"),
    "annika": ("klong prao", "geo"), "elephant bay": ("klong prao", "geo"),
    "sofia": ("klong prao", "geo"), "kp huts": ("klong prao", "geo"),
    "tranquility": ("klong prao", "geo"), "sea-son": ("klong prao", "geo"),
    "sea - son": ("klong prao", "geo"), "sea-sun": ("klong prao", "geo"),
    "chor chaba": ("klong prao", "geo"),
    "mercure": ("bailan beach", "orathai"),   # Hideaway is Bailan Bay (9 Aug)
    "ban klong kok": ("klong prao", "geo"),
    "siam bay": ("lonely beach", "geo"), "siam beach": ("lonely beach", "geo"),
    "nature beach": ("lonely beach", "geo"), "bhumiyama": ("lonely beach", "geo"),
    "bhuriyama": ("lonely beach", "geo"), "oasis": ("lonely beach", "geo"),
    "sunstone": ("lonely beach", "geo"), "sun stone": ("lonely beach", "geo"),
    "slumber": ("lonely beach", "geo"), "cher guesthouse": ("lonely beach", "geo"),
    "magic": ("klong prao", "orathai"),
    "little bungalow": ("lonely beach", "geo"), "fine times": ("lonely beach", "geo"),
    "siam royal": ("white sand beach", "geo"),
    "cliff cottage": ("bang bao pier", "geo"),
    "resolution": ("bang bao pier", "geo"), "indie beach": ("bang bao pier", "geo"),
    "yuyu": ("bang bao pier", "geo"), "klong kloi": ("bang bao pier", "geo"),
    "the beach club": ("bang bao pier", "geo"),
}

_GENERIC_RE = re.compile(r"koh ?chang|kohchang|เกาะช้าง")


def _norm(s):
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def classify(value):
    """Free text → (zone_name, tier, matched_keyword) or (None, None, None).
    tier ∈ {'zone', 'alias-data', 'alias-geo', 'alias-orathai', 'generic'}."""
    v = _norm(value)
    if not v or v in ("-", "na", "n/a", "none"):
        return None, None, None
    for zone, kws in ZONE_KEYWORDS.items():
        for kw in [zone] + kws:
            if kw and kw in v:
                return zone, "zone", kw
    for alias, (zone, ev) in RESORT_ALIASES.items():
        # Word-boundary match: plain substring made "awa" fire inside
        # "hideAWAy" (caught 9 Aug on Mercure Hideaway). Zone keywords
        # above keep substring semantics deliberately (kaibae etc.).
        if re.search(r"\b" + re.escape(alias.strip()) + r"\b", v):
            return zone, f"alias-{ev}", alias.strip()
    if _GENERIC_RE.search(v):
        return "koh chang generic", "generic", "koh chang"
    return None, None, None


# Geocode-upgrade spec (Orathai, 9 Aug): bare zone/resort name -> the zone
# pin IS the final pin; name + extra detail (room number, address...) ->
# zone pin now, flagged 'upgrade-pending' for precise per-booking geocoding
# once an API provider is chosen. Filler tokens don't count as "extra".
_FILLER_RE = re.compile(
    r"\b(resort|hotel|bungalows?|guesthouse|guest|house|beach|koh|chang"
    r"|the|spa|and|pier|airport|terminal|bus)\b"
    r"|[#./()\\,:'&+-]+")
# NOTE deliberately NOT filler: room/rm/no/number and digits — per the
# confirmed spec those count as "extra" and flag upgrade-pending.

def _is_bare_name(norm_value, matched_kw):
    residue = norm_value.replace(matched_kw or "", "")
    residue = _FILLER_RE.sub(" ", residue)
    residue = re.sub(r"\s+", "", residue)
    return len(residue) <= 3


def match_booking(pickup_text, dropoff_text):
    """Returns a dict:
      pickup_zone, pickup_lat, pickup_lng, pickup_precision, pickup_tier,
      dropoff_zone, dropoff_tier, route_key (or Nones).
    route_key derives only when BOTH ends matched. Never raises."""
    out = {"pickup_zone": None, "pickup_lat": None, "pickup_lng": None,
           "pickup_precision": None, "pickup_tier": None,
           "dropoff_zone": None, "dropoff_tier": None, "route_key": None,
           "geocode_precision": None}   # 'final' | 'upgrade-pending'
    try:
        pts = _load_points()
        pz, ptier, pkw = classify(pickup_text)
        dz, dtier, _dkw = classify(dropoff_text)
        if pz and pz in pts:
            out.update(pickup_zone=pz, pickup_tier=ptier,
                       pickup_lat=pts[pz]["lat"], pickup_lng=pts[pz]["lng"],
                       pickup_precision=pts[pz]["precision"])
            out["geocode_precision"] = (
                "final" if _is_bare_name(_norm(pickup_text), pkw)
                else "upgrade-pending")
        elif pz:  # zone known but not in table (shouldn't happen)
            out.update(pickup_zone=pz, pickup_tier=ptier)
        if dz:
            out.update(dropoff_zone=dz, dropoff_tier=dtier)
        if pz and dz:
            out["route_key"] = f"{pz}->{dz}"
    except Exception as e:
        logger.error(f"[MATCHER] match_booking error: {e}")
    return out
