# UTP Partner Revenue Portal — Build Notes

## Commission rule (canonical, owner-confirmed 23 Aug 2026)
Zoho function `calculateUTPCommission` sets `UTP_Commission_Eligible` and
`Commission_Amount` on Koh Chang Orders. Yes + 10% of
`Total_Price_Currency` when the route touches U-Tapao (u-tapao / utapao /
whole-word UTP / อู่ตะเภา, pickup or dropoff) AND Status = Completed.
ALL channels pay — booth, website, LINE, phone, B2B. Partner fields
(`Partner_Introduced_By`, `Partner_Code`, `Partner_Name`) are tracking
data only; they do not gate the money. Refund / any non-Completed status
resets to No + 0. Trigger: workflow rule "UTP Commission Calculation",
Create or Edit, no repeat-on-edit. All three paths live-tested 23 Aug.

## Current state (23 Aug 2026)
`/utp-portal/<24-char token>` serves `utp_portal.html` — a **static demo page
with hardcoded sample figures**. No Zoho connection, no data feed. Shown to
U-Tapao Airport Authority as a design preview. Response carries
`X-Robots-Tag: noindex, nofollow` and the page has a matching robots meta tag.

## Authentication (implemented 23 Aug 2026)

Session login now fronts the portal route — see `portal_auth.py`.
Individual accounts from `PORTAL_USERS` (werkzeug pbkdf2 hashes), Flask
session cookies (Secure/HttpOnly/SameSite=Lax, 30-min sliding expiry),
Thai login page, 5-failure/15-min brute-force block, and a `[PORTAL-AUTH]`
stdout audit line for every login/failure/block/logout (Render logs =
audit trail). Feature-flagged: `PORTAL_AUTH=on` enables it; default off.

**NEVER set env vars via the Render API** — `PUT /env-vars` replaces the
whole collection and has wiped this service before. Paste values in the
Render UI only.

## ⚠️ Known reporting gaps — fix BEFORE the UTP booth opens

Found during the 23 Aug commission-line dry-run. Pre-existing behavior,
deliberately not changed then; all three will bite once UTP volume starts:

1. **Daily 22:30 summary lists Confirmed-created-today only, but UTP
   commission stamps at Completed** — so the daily's "หัก ค่าคอมมิชชั่น
   UTP" lines will rarely fire. Commission-bearing bookings surface only
   in the monthly. Decide: include today's Completed bookings, or accept
   monthly-only visibility.
2. **Monthly P&L filters on `Tour_Date` only.** Transfers use
   `Pickup_Date_Time`, which the monthly ignores — transfers largely
   escape the monthly P&L, which means **UTP commission would too**.
   (July 2026 had zero records with a July Tour_Date; the 1 Aug report
   rendered "ไม่มีข้อมูล".)
3. **On zero-booking days the daily sends nothing** — the n8n Filter
   passes no items so the message node never runs; the "ไม่มี booking
   วันนี้ค่ะ" message is unreachable. A silent night is indistinguishable
   from a broken workflow.

## ⚠️ Remaining preconditions before wiring real booking data

1. Rotate the placeholder accounts `utp-officer-1/2/3` to named
   individuals once the airport names their people (regenerate hashes,
   replace in `PORTAL_USERS`).
2. Confirm password delivery happened **out-of-band** — never in the same
   channel as the portal link.

Related field note: eligibility field API name is `UTP_Commission_Eligible`
(not `Commission_Eligible`) on `Koh_Chang_Orders`.
