# UTP Partner Revenue Portal — Build Notes

## Current state (23 Aug 2026)
`/utp-portal/<24-char token>` serves `utp_portal.html` — a **static demo page
with hardcoded sample figures**. No Zoho connection, no data feed. Shown to
U-Tapao Airport Authority as a design preview. Response carries
`X-Robots-Tag: noindex, nofollow` and the page has a matching robots meta tag.

## ⚠️ Before this page is ever wired to real booking data

The unguessable path is **link-hiding, not authentication**. The airport can
forward the URL to anyone, and anyone holding the link sees everything.
Before connecting real booking data this page MUST get proper authentication
(login, signed expiring links, or IP allow-listing — decided at build time),
not just an obscure path. Rotating the token is not sufficient either; the
same forwarding problem applies to the new token.

Related field note: eligibility field API name is `UTP_Commission_Eligible`
(not `Commission_Eligible`) on `Koh_Chang_Orders`.
