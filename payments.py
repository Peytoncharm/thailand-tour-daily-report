import logging
from datetime import datetime, timezone, timedelta

from zoho_thailand import zoho_search, zoho_get_records

logger = logging.getLogger(__name__)

ICT = timezone(timedelta(hours=7))

TOUR_PACKAGE_TYPES = {"Individual Activity", "Package Activity"}

ORDER_FIELDS = (
    "Name,Last_Name,Tour_Date,Type_of_Package,Package,"
    "Number_of_People,Net_Cost,Provider_List,"
    "Provider_Payment_Status,Status"
)

PROVIDER_FIELDS = (
    "Name,Payment_Trigger,Days_Offset,"
    "Bank_Details,Bank_Account_Number,Bank_Account_Name"
)


def compute_due_date(tour_date, payment_trigger, days_offset):
    """Calculate payment due date from tour date and provider terms."""
    if not payment_trigger or payment_trigger == "-None-":
        return tour_date
    offset = abs(int(days_offset or 0))
    if payment_trigger == "Before Tour":
        return tour_date - timedelta(days=offset)
    elif payment_trigger == "On Tour Date":
        return tour_date
    elif payment_trigger == "After Tour":
        return tour_date + timedelta(days=offset)
    logger.warning(f"[PAYMENTS] Unknown Payment_Trigger: {payment_trigger!r}, defaulting to tour date")
    return tour_date


def fetch_unpaid_orders(today):
    """Fetch Koh_Chang_Orders that are unpaid and have tour dates within ±7 days."""
    window_start = today - timedelta(days=7)
    window_end = today + timedelta(days=7)

    # Fetch all records via paginated GET — Zoho /search doesn't support
    # not_equal on Pick List fields or greater_equal on Date fields.
    # All filtering done in Python.
    logger.info(f"[PAYMENTS] Fetching all Koh_Chang_Orders records...")
    records = zoho_get_records("Koh_Chang_Orders", fields=ORDER_FIELDS)
    logger.info(f"[PAYMENTS] Fetched {len(records)} total records from Zoho")

    # Filter: date window, not cancelled, not paid
    filtered = []
    for r in records:
        tour_date = _parse_tour_date(r)
        if not tour_date:
            continue
        if not (window_start <= tour_date <= window_end):
            continue
        status = (r.get("Status") or "").strip()
        if status == "Cancelled":
            continue
        pps = (r.get("Provider_Payment_Status") or "").strip()
        if pps == "Paid":
            continue
        filtered.append(r)

    logger.info(
        f"[PAYMENTS] {len(records)} total → {len(filtered)} after "
        f"date window ({window_start} to {window_end}), excluding Cancelled/Paid"
    )
    records = filtered

    # Filter to Tour BU package types only
    filtered = []
    skipped = 0
    for r in records:
        pkg_type = (r.get("Type_of_Package") or "").strip()
        if not pkg_type or pkg_type == "-None-":
            skipped += 1
            continue
        if pkg_type in TOUR_PACKAGE_TYPES:
            filtered.append(r)

    if skipped:
        logger.debug(f"[PAYMENTS] Skipped {skipped} orders with empty/None Type_of_Package")
    logger.info(f"[PAYMENTS] {len(filtered)} orders after Tour BU filter")
    return filtered


def fetch_provider_details(provider_names):
    """Lookup provider records by name. Returns dict keyed by provider name."""
    providers = {}
    for name in provider_names:
        if not name:
            continue
        criteria = f"(Name:equals:{name})"
        results = zoho_search("Providers", criteria, fields=PROVIDER_FIELDS)
        if results:
            providers[name] = results[0]
        else:
            logger.warning(f"[PAYMENTS] Provider not found in Zoho: {name!r}")
    logger.info(f"[PAYMENTS] Looked up {len(providers)}/{len(provider_names)} providers")
    return providers


def _parse_tour_date(record):
    """Parse Tour_Date field to date object. Returns None on failure."""
    raw = record.get("Tour_Date") or ""
    if not raw:
        return None
    try:
        if "T" in raw:
            raw = raw.split("T")[0]
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        logger.warning(f"[PAYMENTS] Unparseable Tour_Date: {raw!r}")
        return None


def _provider_name(record):
    """Extract provider name from Provider_List lookup or fallback."""
    pl = record.get("Provider_List")
    if isinstance(pl, dict):
        return (pl.get("name") or "").strip()
    return ""


def _fmt_amount(val):
    if val is None:
        return "0"
    try:
        n = float(val)
        if n == int(n):
            return f"{int(n):,}"
        return f"{n:,.2f}"
    except (ValueError, TypeError):
        return "0"


def compute_due_today(orders, providers, today):
    """Filter orders to those whose payment is due today."""
    due = []
    for order in orders:
        tour_date = _parse_tour_date(order)
        if not tour_date:
            continue

        prov_name = _provider_name(order)
        provider = providers.get(prov_name, {})

        payment_trigger = provider.get("Payment_Trigger", "")
        days_offset = provider.get("Days_Offset", 0)

        if not payment_trigger or payment_trigger == "-None-":
            logger.warning(
                f"[PAYMENTS] No Payment_Trigger for provider {prov_name!r}, "
                f"defaulting to tour date"
            )

        due_date = compute_due_date(tour_date, payment_trigger, days_offset)
        if due_date == today:
            due.append(order)

    logger.info(f"[PAYMENTS] {len(due)} orders due today out of {len(orders)} candidates")
    return due


def compute_overdue(orders, providers, today):
    """Filter orders whose payment due date is before today (overdue)."""
    overdue = []
    for order in orders:
        tour_date = _parse_tour_date(order)
        if not tour_date:
            continue

        prov_name = _provider_name(order)
        provider = providers.get(prov_name, {})

        payment_trigger = provider.get("Payment_Trigger", "")
        days_offset = provider.get("Days_Offset", 0)

        due_date = compute_due_date(tour_date, payment_trigger, days_offset)
        if due_date < today:
            order["_days_overdue"] = (today - due_date).days
            overdue.append(order)

    logger.info(f"[PAYMENTS] {len(overdue)} overdue orders out of {len(orders)} candidates")
    return overdue


def _format_bank_line(provider):
    """Format bank details line. Show all fields as-is, don't guess semantics."""
    bank_acct_name = (provider.get("Bank_Account_Name") or "").strip()
    bank_acct_num = (provider.get("Bank_Account_Number") or "").strip()
    bank_details = (provider.get("Bank_Details") or "").strip()

    if not bank_acct_name and not bank_acct_num and not bank_details:
        return "     \u2753 \u0e44\u0e21\u0e48\u0e21\u0e35\u0e02\u0e49\u0e2d\u0e21\u0e39\u0e25\u0e18\u0e19\u0e32\u0e04\u0e32\u0e23"

    parts = []
    if bank_acct_name:
        parts.append(bank_acct_name)
    if bank_acct_num:
        parts.append(bank_acct_num)

    line = f"     \U0001f3e6 {' | '.join(parts)}" if parts else ""
    if bank_details:
        if line:
            line += f"\n        {bank_details}"
        else:
            line = f"     \U0001f3e6 {bank_details}"
    return line


def _build_provider_section(grouped, providers, show_overdue=False):
    """Build LINE message lines for a group of orders grouped by provider."""
    lines = []
    total = 0
    count = 0

    for prov_name in sorted(grouped.keys()):
        orders = grouped[prov_name]
        provider = providers.get(prov_name, {})

        prov_total = sum(float(o.get("Net_Cost") or 0) for o in orders)
        total += prov_total
        count += len(orders)

        lines.append(
            f"\U0001f3e2 {prov_name} "
            f"({len(orders)} \u0e23\u0e32\u0e22\u0e01\u0e32\u0e23, "
            f"\u0e3f{_fmt_amount(prov_total)})"
        )

        for i, order in enumerate(orders, 1):
            first = (order.get("Name") or "").strip()
            last = (order.get("Last_Name") or "").strip()
            cust_name = f"{first} {last}".strip() if last and last != "-" else first or "Unknown"

            pax = order.get("Number_of_People") or ""
            pkg = order.get("Package") or order.get("Type_of_Package") or ""
            if len(pkg) > 30:
                pkg = pkg[:27] + "..."

            tour_date = _parse_tour_date(order)
            tour_str = tour_date.strftime("%d %b") if tour_date else "?"
            amount = _fmt_amount(order.get("Net_Cost"))

            overdue_tag = ""
            if show_overdue:
                days = order.get("_days_overdue", 0)
                overdue_tag = f" ({days} \u0e27\u0e31\u0e19\u0e17\u0e35\u0e48\u0e41\u0e25\u0e49\u0e27)"

            lines.append(f"  {i}. {cust_name} ({pax} \u0e04\u0e19) {pkg}")
            lines.append(f"     \U0001f4c5 {tour_str} | \u0e3f{amount}{overdue_tag}")
            lines.append(_format_bank_line(provider))

        lines.append("")

    return lines, total, count


def build_payments_report(due_orders, overdue_orders, providers, today):
    """Build the LINE message grouped by provider, with overdue section."""
    if not due_orders and not overdue_orders:
        date_str = today.strftime("%d %b %Y")
        return (
            f"\u2705 \u0e44\u0e21\u0e48\u0e21\u0e35\u0e23\u0e32\u0e22\u0e01\u0e32\u0e23"
            f"\u0e08\u0e48\u0e32\u0e22\u0e40\u0e07\u0e34\u0e19 Provider \u0e27\u0e31\u0e19\u0e19\u0e35\u0e49\n"
            f"\U0001f4c5 {date_str}"
        )

    date_str = today.strftime("%d %b %Y")
    lines = [
        f"\U0001f4b0 PROVIDER PAYMENTS DUE \u2014 {date_str}",
        "\u2501" * 15,
        "",
    ]

    grand_total = 0
    grand_count = 0

    # Due today section
    if due_orders:
        grouped = {}
        for order in due_orders:
            prov_name = _provider_name(order) or "Unknown Provider"
            grouped.setdefault(prov_name, []).append(order)

        section_lines, section_total, section_count = _build_provider_section(grouped, providers)
        lines.extend(section_lines)
        grand_total += section_total
        grand_count += section_count

    # Overdue section
    if overdue_orders:
        grouped = {}
        for order in overdue_orders:
            prov_name = _provider_name(order) or "Unknown Provider"
            grouped.setdefault(prov_name, []).append(order)

        lines.append("\u26a0\ufe0f \u0e04\u0e49\u0e32\u0e07\u0e08\u0e48\u0e32\u0e22 (OVERDUE)")
        lines.append("\u2501" * 15)
        lines.append("")

        section_lines, section_total, section_count = _build_provider_section(
            grouped, providers, show_overdue=True
        )
        lines.extend(section_lines)
        grand_total += section_total
        grand_count += section_count

    lines.append("\u2501" * 15)
    lines.append(
        f"\U0001f4b0 TOTAL: {grand_count} \u0e23\u0e32\u0e22\u0e01\u0e32\u0e23 | "
        f"\u0e3f{_fmt_amount(grand_total)}"
    )
    lines.append("")
    lines.append(
        "\u0e08\u0e48\u0e32\u0e22\u0e41\u0e25\u0e49\u0e27 \u2192 "
        "mark Provider Payment Status = Paid \u0e43\u0e19 Zoho"
    )

    return "\n".join(lines).strip()


def run_daily_payments():
    """Main entry point: fetch, compute, build report. Returns (message, stats)."""
    today = datetime.now(ICT).date()
    logger.info(f"[PAYMENTS] Running for date: {today}")

    orders = fetch_unpaid_orders(today)
    if not orders:
        message = build_payments_report([], [], {}, today)
        return message, {"orders_found": 0, "orders_due_today": 0, "overdue": 0, "providers": 0}

    # Unique provider names
    provider_names = list({_provider_name(o) for o in orders} - {""})
    providers = fetch_provider_details(provider_names)

    due_orders = compute_due_today(orders, providers, today)
    overdue_orders = compute_overdue(orders, providers, today)
    message = build_payments_report(due_orders, overdue_orders, providers, today)

    all_flagged = due_orders + overdue_orders
    stats = {
        "orders_found": len(orders),
        "orders_due_today": len(due_orders),
        "overdue": len(overdue_orders),
        "providers": len({_provider_name(o) for o in all_flagged} - {""})
    }
    return message, stats
