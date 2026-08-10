# Early-Pickup Positioning Strategy — Transfer BU
**Drafted 10 August 2026 · Orathai + Claude · for Stage 2 spec integration**

The problem in one sentence: for early-morning pickups, the job is won or lost
the night before — by where the driver sleeps — and no amount of morning
monitoring can fix a driver who is on the wrong side of the water.

---

## 1. THE PHYSICAL CONSTRAINTS (the facts the system must know)

| Constraint | Value (to verify with Orathai) | Consequence |
|---|---|---|
| First ferry Ao Thammachat → island | ~06:00–06:30 | Mainland driver's earliest island arrival ~07:30–08:00 |
| Last ferry island → mainland | ~19:00 (evening) | Island driver can't serve pre-dawn mainland departures unless he crossed the evening before |
| Crossing time | ~45 min | Fixed segment |
| Queue variability | 30 min – 3.5 hrs | Holiday mornings can push "earliest island arrival" past 09:00 |
| Bangkok → Trat drive | ~4–4.5 hrs | A 05:00 Suvarnabhumi pickup from Trat means a ~00:30 departure — feasible but must be knowingly accepted |

**Rule of thumb the system encodes:**
- Island pickup before ~08:30 → driver must sleep ON the island
- Mainland pickup before ~07:00 in Trat/Laem Ngob area → driver must sleep MAINLAND-side
- These cutoffs are DATA (editable), not code — they shift with the ferry timetable and season

---

## 2. THE THREE-LAYER DEFENSE

### Layer 1 — Feasibility gate at ASSIGNMENT time (prevents the trap)
When a booking is created, compute its **positioning requirement**:

```
IF pickup_zone is island AND pickup_time < island_cutoff (default 08:30):
    positioning_required = "island overnight"
    position_deadline   = last ferry, evening before
IF pickup_zone is mainland-near-ferry AND pickup_time < mainland_cutoff (default 07:00):
    positioning_required = "mainland overnight"
```

The Driver Matching broadcast for such jobs states it explicitly in the LINE
message: **"งานเช้าเกาะช้าง — ต้องค้างบนเกาะคืนก่อน"** (early island job —
must stay on the island the night before). A driver who accepts is accepting
the overnight positioning. No hidden surprises, no morning heroics.

### Layer 2 — Evening-before VERIFICATION (GPS earns its keep)
A new nightly check at ~20:00 for every next-morning early pickup with an
assigned driver:

| Driver's last GPS position at 20:00 | Action |
|---|---|
| Correct side of the water | Silence — all fine |
| Wrong side, or no signal | Automated LINE nudge to the DRIVER: "พรุ่งนี้งานเช้าบนเกาะ — ข้ามเรือคืนนี้หรือยังครับ?" |
| Still wrong side / dark at ~21:00 (before ferries stop) | TEAM ALERT — hours remain to reassign to a correctly-positioned driver |

This converts the failure mode from *"discovered at 05:30, customer stranded,
zero options"* to *"flagged at 20:30 the night before, reassignment window
open"*. This check alone justifies the GPS programme for early jobs.

Design notes:
- Uses driver_latest (already live) + the positioning fields from Layer 1
- Respects the no-signal reality: a dark driver gets the nudge, not an
  instant team alert — many drivers won't be tracking off-duty (duty-cycle
  model, decided 10 Aug)
- The driver's LINE REPLY is an accepted confirmation channel: "อยู่บนเกาะแล้วครับ"
  closes the check even without GPS (reply-based confirmation is free on LINE)
- Counts against the 20/day alert budget only at the team-alert stage

### Layer 3 — The matcher learns GEOGRAPHY (prevents bad assignments)
- New Provider field: **Home_Base** (island / mainland / bangkok / other) —
  filled progressively, starting with the top 30 regulars (Orathai/team
  knowledge, ~30 minutes)
- Early island jobs broadcast **island-based drivers FIRST** (round 1),
  mainland drivers only in later rounds with the overnight warning prominent
- Over time, GPS history can infer home base automatically (where does this
  driver's day start?) — a later enhancement, not needed for launch

---

## 3. THE COMMERCIAL LAYER (Orathai/boss decisions — software can't fix pricing)

An overnight positioning costs the driver: a night away from home or a
pre-dawn start. If the job doesn't compensate it, drivers will quietly
decline and broadcasts will time out — a bottleneck invisible to software.

Decisions needed:
1. **Positioning allowance** for early island pickups? (flat ฿ add-on, or
   built into the early-morning rate tier)
2. **Job bundling**: an early island pickup pairs naturally with a
   previous-evening island dropoff — the matcher could PREFER drivers who
   already have an evening job ending island-side (a free positioning).
   This is the elegant solution: sell the evening job and the morning job
   as a natural pair.
3. **Cancellation policy** for positioned jobs: if a customer cancels at
   21:00 and the driver has already crossed, who bears the ferry cost?

Recommendation: start with bundling preference (costs nothing, uses data we
have) + a modest positioning allowance for unbundled early island jobs.

---

## 4. WHAT THE CUSTOMER-FACING SIDE SHOULD DO (defensive booking)

- Website/booking flow: island pickups before the feasibility cutoff show a
  notice or require confirmation — "early pickups on Koh Chang are served by
  island-based drivers; availability is limited" — setting expectation and
  justifying any rate difference
- Consider a booking-time warning to the TEAM for any early island booking
  created inside 24 hours of pickup (the hardest to position for)

---

## 5. BUILD SEQUENCE (folds into Stage 2)

| Step | What | Depends on |
|---|---|---|
| A | Ferry cutoff constants as editable data (ferry_model.json) | Orathai confirms timetable |
| B | Positioning computation at booking-cache time (island/mainland + deadline) | A + pickup zones (live) |
| C | Broadcast message variant for positioning-required jobs | B |
| D | 20:00 evening verification check + nudge/alert ladder | B + driver_latest (live) |
| E | Home_Base field + island-first broadcast ordering | Team fills top 30 |
| F | Bundling preference in matcher | E + evening dropoff data |

Steps A–D are system work (Claude Code, gated as usual). E needs ~30 min of
team knowledge. F is the optimization layer.

---

## 6. OPEN QUESTIONS FOR ORATHAI (needed before build)

1. Exact current ferry timetable: first/last crossing both directions
2. The island cutoff: is 08:30 right, or with queue risk should it be 09:00?
3. How many early island pickups actually occur? (historical data can answer
   — worth a query before sizing the build)
4. Positioning allowance: yes/no/amount — or bundling-only to start?
5. Do any drivers currently LIVE on the island? (If several do, Layer 3 is
   mostly a labelling exercise and the problem shrinks dramatically)
