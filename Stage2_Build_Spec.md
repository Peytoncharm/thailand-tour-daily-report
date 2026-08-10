# Stage 2 Build Spec — ETA Engine + Ferry Model + Alert Engine

Draft v1.0 — 9 Aug 2026, ~23:00 ICT — prepared by Claude for Orathai's morning review.
DOCUMENT ONLY: nothing in this file is built, deployed, or approved yet.

---

## 0a. DECISIONS LOCKED (Orathai sitting, 10 Aug ~12:3x — mapped by content; original labels arrived shuffled)

| Decision | Locked value |
|---|---|
| D1 Routing API | **Google** (billing setup deferred; code stays provider-agnostic behind get_route_eta) |
| D4 Alert recipients / quiet hours | **Team ops LINE group; NO quiet hours** |
| D5 Budget overflow at 20/day cap | **Digest mode** (suppressed alerts collected into a digest line) |
| Go-live policy | **Shadow until an evidence gate on real bookings** (per-step gates in §3 stand) |
| NEW — signal-age escalation ladder | **20/30/45 min: 20=stale (consistent with D7 1200s) → 30=driver nudge → 45=team alert; suppressed during detected ferry crossing.** *Interpretation flagged — confirm in one line.* |
| D-P1 Ferry constants | **First 06:30 / last 18:30; positioning deadline = 17:45 boat the evening before** — Orathai-provided values (NOT web-verified yet; check against operator schedule before P-A gate closes) |
| D-P2 Island cutoff | **09:00** (caseload ≈132 island pickups/34% — accepted) |
| D-P3 Commercial | **Bundling-first, no positioning allowance yet** |
| D-P4 Home_Base | **Field now; fill island residents only** (rest default unknown) |

**STILL OPEN: D2 — stage-1 filter constants** (straight-line ÷45 km/h, pass margin 1.6×): proposed defaults stand, revisited against shadow data (Step 3 gate).
**EXPLICITLY NOT FOLDED: "Ao Sapparot restoration"** — contradicts the standing record (declared permanently closed 9 Aug; validation asserts absence; Thammachat is the only car ferry). Requires a fresh explicit instruction with effective date before any change to zones, ferry model, or validation.

## 0. OPEN DECISIONS — need Orathai's input before the affected step starts

| # | Decision | Affects | Options / notes |
|---|---|---|---|
| D1 | **Routing API provider** | Step 4 (stage-2 paid check), later geocode-upgrade queue | Google Routes vs OSRM (self-host, free) vs OpenRouteService vs Longdo (Thai). Deferred by Orathai on 9 Aug ("closer to live traffic"). Spec is written provider-agnostic behind one function. |
| D2 | **Stage-1 filter constants** | Step 3 | Proposed: straight-line ÷ 45 km/h, pass if time-remaining > 1.6× estimate. Honest note: today's data cannot beat these guesses — eta_history is empty until shadow mode runs. Proposal: adopt as-is, revisit after 2 weeks of shadow data (Step 3 gate includes producing that comparison). |
| D3 | **Ferry timetable + queue baselines** (editable data file) | Step 5 | I can pre-fill from public Ao Thammachat schedules, but Orathai's operational knowledge (real sailing gaps, high-season queue patterns) should overwrite before go-live. Format below §3. |
| D4 | **Alert recipients & quiet hours** | Step 7 | Which LINE group gets each alert class; do alerts sleep 23:00–06:00 except no-signal-before-imminent-pickup? |
| D5 | **20/day alert budget overflow behaviour** | Step 7 | When the daily cap is hit: silent drop + morning digest line (proposed) vs one "budget exhausted" meta-alert then silence. |
| D6 | **Route_Key baseline — ANSWERED by measurement (Orathai's rule: "whatever it measures is the number", 9 Aug ~23:15)** | baseline stats | Final matcher (25 zones, trat-airport keywords, bailan, orathai corrections, boundary fix) vs all history: **ALL 807 bookings — pickup 86.3%, dropoff 65.0%, route derivable 54.8%. TRANSFER-only (337) — pickup 87.5%, dropoff 56.4%, route derivable 47.8%.** The 97.2% figure from the brief is not reproducible on either denominator. Note the inversion: transfer dropoffs match WORSE than tours (transfers end at Bangkok hotels / island resorts — the unmatchable long tail; tours end where they start). These are the gate-quotable baselines; the per-booking geocode-upgrade queue (D1) is what raises them. |
| D7 | **Stale threshold — ANSWERED: 1200 s approved** (Orathai, 9 Aug: Traccar configured 60 s interval / 100 m filter / **900 s stationary heartbeat** at setup) | Step 6 | Locked: position older than 1200 s (heartbeat + 300 s grace) = no-signal, never "stationary". |

---

## 1. Ground truth this spec builds on (verified live, 9 Aug 2026)

- **booking_cache** (Postgres): every active booking, raw Zoho payload + extracted columns incl. `pickup_lat/lng`, `geocode_precision` ('final' | 'upgrade-pending'), refreshed ≤15 min by sweep + instant on-create webhook. Cache-first reads everywhere; Zoho fallback verbatim.
- **pickup_points**: 25 zones with `precision` tier — `exact` (airports/piers/terminals incl. trat airport, laem sok, ao thammachat), `zone` (beach/town centroids incl. bailan), `generic` (koh chang island catch-all @ 12.05,102.29).
- **pickup_matcher**: word-boundary alias matching, orathai-tier corrections, geocode-upgrade flagging per confirmed spec.
- **eta_history** (empty, ready): `booking_id, driver_id, route_key, computed_at, distance_km, predicted_sec, actual_sec, method, on_time` + indexes on (booking_id, computed_at) and (route_key, computed_at) — the self-correction loop's table.
- **alert_log** (empty, ready): `booking_id, driver_id, alert_type, sent_at, channel, detail`, indexed.
- **driver_latest** (live): one UPSERTed row per driver — position, speed, bearing, ts, batt.
- **Route_Key**: derived pickup-zone→dropoff-zone (Transfer_Route is 99% blank in Zoho); written to Zoho + cache per booking.
- **Checkpoint cadence** (per architecture): T-120 / T-90 / T-60 / T-30 minutes before pickup, riding the existing 15-min cron pattern.
- **Deploy safety**: readiness-gated deploys (`/ready` + healthCheckPath) — no cron cycle lost on deploy; health endpoint exposes commit + pickup_points count for external verification.
- **Provider guard**: KCE/SWB/Garfield never receive GPS/watchdog output (two-tier policy).

## 2. Architecture in one paragraph

Every 15 minutes a checkpoint pass reads today's bookings from booking_cache (zero Zoho reads), pairs each with `driver_latest` for its assigned driver, and runs the **two-stage ETA check**: stage 1 is a free geometric filter that clears the obvious on-time majority; only at-risk jobs proceed to stage 2, a paid routing-API call (D1) segmented by the **ferry model** when the route crosses to/from the island. Every prediction is written to `eta_history` (`predicted_sec`, later `actual_sec` on completion) keyed by `route_key`, so per-route correction factors improve both stages over time. Rule breaches feed the **alert engine**, which dedups via `alert_log`, respects precision tiers, and enforces the 20/day budget. Every step ships in **shadow mode first** (log what WOULD happen, send nothing).

## 3. The build — gated, independently verifiable steps

### Step 1 — Checkpoint skeleton (shadow)
One new cron endpoint `/cron/eta-checkpoints` (15-min cadence, existing gate/auth pattern). For each booking in today's cache with a matched pickup: compute which checkpoint window (T-120/90/60/30) it is in, join `driver_latest`, and **log only** — no math yet, no messages.
**Gate:** two clean cron cycles; log lines show correct checkpoint classification for ≥1 real booking (or a dummy). **Shadow: yes (inherently).**

### Step 2 — actual_sec completion writer
When a booking's dropoff time passes (or driver_latest shows arrival at destination zone, whichever is observable), close open eta_history rows with `actual_sec`. Without this, the learning loop never closes.
**Gate:** dummy booking's eta_history row gains a plausible actual_sec. **Shadow: n/a (writes only to our own DB).**

### Step 3 — Stage-1 free filter (shadow)
[PROPOSED DEFAULT — D2] `straight_line_km(driver_latest → pickup pin) ÷ 45 km/h = est_sec`; **pass** if `time_to_pickup > 1.6 × est_sec`. Ferry-crossing routes (mainland↔island, §4) automatically fail stage 1 into stage 2 — straight-line over water is meaningless. Every evaluation writes eta_history (`method='straight-line'`).
**Gate:** after ≥3 days of shadow data: false-pass rate (passed stage 1 but actually late) reported; constants revisited against real numbers (D2 loop).
**Shadow: yes** — flags logged, nothing sent.

### Step 4 — Stage-2 paid routing check (shadow, provider-gated on D1)
Only jobs failing stage 1. One `get_route_eta(from, to)` function wrapping the chosen provider (D1); segmented via ferry model when applicable; result written to eta_history (`method='road'` / `'road+ferry'`). Per-route correction factor applied: `corrected = predicted × median(actual/predicted over last N=10 completions on this route_key)` [PROPOSED DEFAULT: median, N=10, only when ≥5 samples].
**Gate:** dummy at-risk booking produces a stage-2 row with sane numbers; daily API call count visible in the cron JSON (cost control).
**Shadow: yes.**

### Step 5 — Ferry model (data + code)
**Ao Thammachat ↔ Koh Chang is the ONLY car ferry** (Ao Sapparot and Centerpoint permanently closed — already removed from pickup_points). Four segments: `drive_to_pier + queue + crossing + island_leg`.
Timetable + queue baselines are **editable data, not code** — `ferry_model.json` in the repo [D3, Orathai overwrites my pre-fill]:
```json
{ "pier_mainland": "ao thammachat pier", "pier_island": "ao sapparot IS CLOSED — island pier row TBD",
  "sailings": {"first": "06:00", "last": "19:00", "interval_min": 45},
  "crossing_min": 30,
  "queue_min_baseline": {"default": 15, "high_season": 40, "weekend_peak": 60} }
```
**Queue-learning loop:** from `driver_latest`, detect pier-arrival (position within R=300 m of pier pin, speed ≈ 0) and mid-water (position over the strait / speed+bearing consistent with crossing); `measured_queue_sec = t(mid-water) − t(pier-arrival) − crossing` → written to eta_history (`method='ferry-queue-observed'`) keyed by a pseudo route_key `ferry-queue:<weekday>:<hour>`; baselines report drift vs the JSON monthly.
**Gate:** replay of any real crossing in `driver_positions` history yields a plausible measured queue; JSON edits require no deploy to take effect [PROPOSED: reload per cron pass].
**Shadow: yes** — measurements collected without affecting predictions until Orathai approves the observed baselines.

### Step 6 — Precision-aware claim limits (policy enforced in code)
| Pin precision | Checkpoint may claim | Never |
|---|---|---|
| exact | full two-stage ETA, minute-level | — |
| zone | two-stage ETA with ±10 min band [PROPOSED]; alerts phrased as "at risk", not "X min late" | minute-level promises |
| generic | coarse only: driver moving/not-moving, on-island/off-island, no-signal | ANY minute-level ETA claim (Orathai decision 1, 9 Aug) |
`geocode_precision='upgrade-pending'` inherits its zone tier until upgraded. **Stale-signal threshold** [D7 — PROPOSED 1200 s]: below it, position is "current"; above it, rules must treat the driver as no-signal, not as stationary — parked drivers with distance-filtered heartbeats must not false-alarm.
**Gate:** unit-style table test — every (precision × rule) combination produces the permitted claim class only.

### Step 7 — Alert engine (shadow → live)
**Four GPS rules** (evaluated per checkpoint, all shadow first):
1. **No-signal** — assigned, tracker-known driver has no fresh position inside T-120 (distinct alert type `no-signal`, NOT `late`).
2. **Not-departed** — T-60/T-30: driver stationary in a zone inconsistent with reaching pickup (stage-1/2 math), precision-gated per Step 6.
3. **At-risk ETA** — stage-2 corrected ETA exceeds time-to-pickup minus buffer [PROPOSED buffer: 10 min].
4. **Wrong-direction** — bearing/position diverging from pickup for 2 consecutive checkpoints [PROPOSED].
**Dedup:** before sending, query alert_log for same (booking_id, alert_type) within [PROPOSED 90 min]; suppressed repeats logged with `channel='suppressed'`.
**Budget:** hard 20 alerts/day across all types (count from alert_log); overflow per D5. Provider guard consulted before any driver-facing message (team alerts unaffected).
**Gate:** ≥3 days shadow with daily "would-have-alerted" digest reviewed by Orathai; false-positive rate acceptable to her; THEN live flag (env var, same kill-switch pattern as GEOCODE_ENABLED).
**Shadow: yes — the whole engine ships dark behind `ALERTS_ENABLED`.**

### Step 8 — Go-live sequencing
Shadow everything ≥3 days → Orathai reviews digests → enable alerts for `exact`-precision bookings only → widen to `zone` after 1 clean week → `generic` stays coarse forever. Each widening is an env-var change, no deploy.

## 4. Rollback story (uniform)
Every engine sits behind its own env var (`ETA_CHECKPOINTS_ENABLED`, `ALERTS_ENABLED`); OFF restores today's exact behaviour. All writes are to our own Postgres tables; Zoho is untouched by Stage 2 except existing flag writes. Deploys are readiness-gated; no cron loss.

## 4b. Early-Pickup Positioning (folded 10 Aug from Early_Pickup_Positioning_Strategy.md)

**Morning decisions (Orathai, 10 Aug) now binding on this spec:**
- **GPS boxes ELIMINATED** — freelance vehicles; phone-app (Traccar) tracking only. No hardware line item anywhere.
- **Duty-cycle tracking model** — drivers track on working days, may be dark off-duty. Consequence: no-signal is NEVER treated as misbehaviour outside an assigned-job window; all no-signal rules key off assignments, not presence.
- **Early-pickup alert suppression until T-90** — for positioning-required jobs the morning checkpoint alerts stay silent before T-90; the evening-before check (Step P-D) is the real defense, mornings only confirm.
- **Nudge-before-team-alert ladder** — driver LINE nudge first (~20:00), team alert only if still wrong-side/dark by ~21:00 (before ferries stop). Driver's LINE reply ("อยู่บนเกาะแล้วครับ") closes the check without GPS. Team-alert stage only counts against the 20/day budget.

**Problem size (measured 10 Aug, 454 bookings with pickup times, 387 island-side):**
island pickups before 08:00 = 19 (5%) · before 09:00 = **132 (34%)** · before 10:00 = 233 (60%). Peak hours 08:xx (113) and 09:xx (101). The doc's rule-of-thumb cutoff 08:30 sits mid-peak — the <08:30 band is roughly 19 + half of the 08:xx block ≈ 75 bookings (~19%), a real but bounded problem; D-P2 (cutoff choice) materially changes the caseload.

**Gated build steps (extends §3; same discipline):**
| Step | What | Gate | Shadow? |
|---|---|---|---|
| P-A | Ferry cutoff constants join `ferry_model.json` (island_cutoff default 08:30, mainland_cutoff 07:00 — DATA not code) | Orathai confirms timetable (D-P1) | n/a |
| P-B | Positioning computation at booking-cache time: `positioning_required` + `position_deadline` on the cache row | dummy early-island booking gets correct requirement + deadline | yes (fields only) |
| P-C | Broadcast message variant "งานเช้าเกาะช้าง — ต้องค้างบนเกาะคืนก่อน" for positioning-required jobs | dummy broadcast rendering | yes |
| P-D | 20:00 evening verification: driver_latest side-of-water check → nudge ladder per the morning decisions | shadow digest of would-nudge/would-alert reviewed by Orathai before live | **yes** |
| P-E | **NEEDS ORATHAI**: `Home_Base` Provider field + top-30 fill (~30 min team knowledge) + island-first broadcast ordering | field created (gate: provider field count +1), 30 regulars filled | — |
| P-F | Bundling preference (evening island dropoff pairs with morning island pickup) | offline simulation on history first | yes |

**Commercial layer — flagged, Orathai/boss inputs, software waits:** positioning allowance (yes/no/amount), bundling as pricing strategy, cancellation policy for positioned jobs (doc §3). Customer-facing early-booking notice (doc §4) is also a business call.

**Open decisions added:** D-P1 exact ferry timetable both directions (feeds D3) · D-P2 island cutoff 08:30 vs 09:00 under queue risk (~caseload 75 vs ~132) · D-P3 positioning allowance / bundling-only start · D-P4 which drivers live island-side (shrinks Layer 3 to labelling).

## 5. Explicitly out of scope for Stage 2
Historical backfill (fenced, separate approval) · n8n migration · driver rollout guide & Android testing (parked pending GPS verdict) · geocode-upgrade API execution (flag already collected; blocked on D1).
