import os
import logging
from datetime import datetime, timedelta, date
from calendar import monthrange
from zoneinfo import ZoneInfo

from zoho_thailand import zoho_coql, zoho_search

logger = logging.getLogger(__name__)

ICT = ZoneInfo("Asia/Bangkok")

LINE_TOKEN = os.environ.get("KOHCHANG_LINE_TOKEN", "")
LINE_GROUP = os.environ.get("RECONCILIATION_LINE_GROUP_ID", "")


# ---------------------------------------------------------------------------
# Zoho queries
# ---------------------------------------------------------------------------

def _fetch_unpaid_orders():
    """Fetch unpaid orders with providers, Tour_Date >= 60 days ago."""
    cutoff = (datetime.now(ICT).date() - timedelta(days=60)).strftime("%Y-%m-%d")
    query = (
        "SELECT Name, Tour_Date, Type_of_Package, Provider_List, "
        "Provider_Payment_Status, Net_Cost, Total_Net_Cost_Currency, "
        "Adults1, Children1, Chanel_of_booking, Created_Time "
        "FROM Koh_Chang_Orders "
        f"WHERE (Provider_Payment_Status = 'Pending' or Provider_Payment_Status = 'Disputed' or Provider_Payment_Status is null) "
        "AND Provider_List is not null "
        f"AND Tour_Date >= '{cutoff}' "
        "ORDER BY Tour_Date desc "
        "LIMIT 200"
    )
    records = zoho_coql(query)
    # Filter out TEST bookings
    filtered = [
        r for r in records
        if (r.get("Chanel_of_booking") or "").upper() != "TEST"
    ]
    logger.info(f"[PAY-REG] Unpaid orders: {len(records)} raw, {len(filtered)} after TEST filter")
    return filtered


def _fetch_paid_yesterday():
    """Fetch orders marked Paid that were modified yesterday (ICT)."""
    now_ict = datetime.now(ICT)
    yesterday = now_ict.date() - timedelta(days=1)
    start = f"{yesterday}T00:00:00+07:00"
    end = f"{yesterday}T23:59:59+07:00"
    query = (
        "SELECT Name, Tour_Date, Type_of_Package, Provider_List, "
        "Provider_Payment_Status, Net_Cost, Total_Net_Cost_Currency, "
        "Chanel_of_booking, Modified_Time "
        "FROM Koh_Chang_Orders "
        "WHERE Provider_Payment_Status = 'Paid' "
        "AND Provider_List is not null "
        f"AND Modified_Time >= '{start}' "
        f"AND Modified_Time <= '{end}' "
        "ORDER BY Modified_Time desc "
        "LIMIT 100"
    )
    records = zoho_coql(query)
    filtered = [
        r for r in records
        if (r.get("Chanel_of_booking") or "").upper() != "TEST"
    ]
    logger.info(f"[PAY-REG] Paid yesterday: {len(records)} raw, {len(filtered)} after TEST filter")
    return filtered


# ---------------------------------------------------------------------------
# Provider lookup
# ---------------------------------------------------------------------------

def _fetch_providers(provider_ids):
    """Batch-fetch provider records by ID. Returns dict keyed by provider ID."""
    if not provider_ids:
        return {}

    providers = {}
    # COQL IN clause — batch up to 50 at a time
    id_list = list(provider_ids)
    for i in range(0, len(id_list), 50):
        batch = id_list[i:i + 50]
        in_clause = ", ".join(f"'{pid}'" for pid in batch)
        query = (
            "SELECT Name, Payment_Trigger, Days_Offset, "
            "Bank_Details, Bank_Account_Number, Bank_Account_Name "
            f"FROM Providers WHERE id in ({in_clause})"
        )
        records = zoho_coql(query)
        for r in records:
            pid = r.get("id", "")
            if pid:
                providers[pid] = r

    logger.info(f"[PAY-REG] Fetched {len(providers)}/{len(provider_ids)} providers")
    return providers


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------

def _parse_date(val):
    """Parse a date string (YYYY-MM-DD or with T) to date object, or None."""
    if not val:
        return None
    try:
        raw = val.split("T")[0] if "T" in val else val
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def _compute_due_date(order, provider):
    """Compute payment due date for an order based on provider's Payment_Trigger."""
    tour_date = _parse_date(order.get("Tour_Date"))
    if not tour_date:
        return None

    trigger = (provider.get("Payment_Trigger") or "").strip()
    offset = provider.get("Days_Offset")

    if not trigger or trigger == "-None-":
        return tour_date  # default: due on tour date

    if trigger == "On Tour Date":
        return tour_date

    if trigger == "After Tour":
        days = abs(int(offset or 0))
        return tour_date + timedelta(days=days)

    if trigger == "Pay on Booking Date":
        created = _parse_date(order.get("Created_Time"))
        return created if created else tour_date

    if trigger == "Bi-Monthly Cycle":
        if tour_date.day <= 15:
            return tour_date.replace(day=16)
        else:
            next_month = tour_date.replace(day=1) + timedelta(days=32)
            return next_month.replace(day=1)

    if trigger == "Before Tour":
        days = abs(int(offset or 0))
        return tour_date - timedelta(days=days)

    return tour_date  # unknown trigger → default


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

def _get_provider_info(order):
    """Extract provider id and name from order's Provider_List lookup."""
    pl = order.get("Provider_List")
    if isinstance(pl, dict):
        return pl.get("id", ""), (pl.get("name") or "").strip()
    return "", ""


def _get_amount(order):
    """Get payment amount from order. Returns (float, str) or (None, display_str)."""
    net = order.get("Net_Cost")
    if net is not None:
        try:
            val = float(net)
            return val, _fmt_amount(val)
        except (ValueError, TypeError):
            pass
    alt = order.get("Total_Net_Cost_Currency")
    if alt is not None:
        try:
            val = float(alt)
            return val, _fmt_amount(val)
        except (ValueError, TypeError):
            pass
    return None, "(\u0e44\u0e21\u0e48\u0e23\u0e30\u0e1a\u0e38)"  # (ไม่ระบุ)


def _fmt_amount(val):
    """Format number as Thai baht string."""
    if val is None:
        return "0"
    try:
        n = float(val)
        if n == int(n):
            return f"{int(n):,}"
        return f"{n:,.2f}"
    except (ValueError, TypeError):
        return "0"


def _classify_orders(orders, providers, today):
    """Split orders into due_today and overdue lists."""
    due_today = []
    overdue = []

    for order in orders:
        prov_id, prov_name = _get_provider_info(order)
        provider = providers.get(prov_id, {})
        due_date = _compute_due_date(order, provider)

        if due_date is None:
            continue

        if due_date == today:
            due_today.append(order)
        elif due_date < today:
            order["_days_overdue"] = (today - due_date).days
            overdue.append(order)

    logger.info(f"[PAY-REG] Classified: {len(due_today)} due today, {len(overdue)} overdue")
    return due_today, overdue


def _detect_duplicates(orders, providers):
    """Detect potential duplicate payments: same provider + same amount on same day."""
    seen = {}
    duplicates = set()

    for order in orders:
        prov_id, _ = _get_provider_info(order)
        amount, _ = _get_amount(order)
        if amount is None:
            continue
        key = (prov_id, amount)
        if key in seen:
            duplicates.add(order.get("id", ""))
            duplicates.add(seen[key])
        else:
            seen[key] = order.get("id", "")

    if duplicates:
        logger.warning(f"[PAY-REG] Potential duplicates detected: {len(duplicates)} orders")
    return duplicates


# ---------------------------------------------------------------------------
# Message formatting
# ---------------------------------------------------------------------------

def _format_pax(order):
    """Format passenger count like '2+1 pax' or '2 pax'."""
    adults = order.get("Adults1") or 0
    children = order.get("Children1") or 0
    try:
        adults = int(adults)
    except (ValueError, TypeError):
        adults = 0
    try:
        children = int(children)
    except (ValueError, TypeError):
        children = 0
    if children > 0:
        return f"{adults}+{children} pax"
    return f"{adults} pax"


def _format_bank(provider):
    """Format bank details line."""
    bank = (provider.get("Bank_Details") or "").strip()
    acct_num = (provider.get("Bank_Account_Number") or "").strip()
    acct_name = (provider.get("Bank_Account_Name") or "").strip()

    if not bank and not acct_num and not acct_name:
        return "      \U0001f3e6 (\u0e44\u0e21\u0e48\u0e21\u0e35\u0e02\u0e49\u0e2d\u0e21\u0e39\u0e25\u0e18\u0e19\u0e32\u0e04\u0e32\u0e23)"

    parts = []
    if bank:
        parts.append(bank)
    if acct_num:
        parts.append(acct_num)
    line = f"      \U0001f3e6 {' '.join(parts)}"
    if acct_name:
        line += f" ({acct_name})"
    return line


def _format_thai_date(d):
    """Format date as Thai-style short date like '1 พ.ค.'."""
    if d is None:
        return "?"
    thai_months = {
        1: "\u0e21.\u0e04.", 2: "\u0e01.\u0e1e.", 3: "\u0e21\u0e35.\u0e04.",
        4: "\u0e40\u0e21.\u0e22.", 5: "\u0e1e.\u0e04.", 6: "\u0e21\u0e34.\u0e22.",
        7: "\u0e01.\u0e04.", 8: "\u0e2a.\u0e04.", 9: "\u0e01.\u0e22.",
        10: "\u0e15.\u0e04.", 11: "\u0e1e.\u0e22.", 12: "\u0e18.\u0e04.",
    }
    return f"{d.day} {thai_months.get(d.month, '')}"


def _format_thai_date_full(d):
    """Format date as '1 พ.ค. 2026'."""
    if d is None:
        return "?"
    return f"{_format_thai_date(d)} {d.year}"


def _build_provider_section(orders, providers, duplicates, show_overdue=False):
    """Build LINE message lines for orders grouped by provider."""
    # Group by provider
    grouped = {}
    for order in orders:
        prov_id, prov_name = _get_provider_info(order)
        prov_name = prov_name or "Unknown Provider"
        grouped.setdefault((prov_id, prov_name), []).append(order)

    lines = []
    total_amount = 0
    total_count = 0

    for (prov_id, prov_name), prov_orders in sorted(grouped.items(), key=lambda x: x[0][1]):
        provider = providers.get(prov_id, {})
        prov_total = 0
        for o in prov_orders:
            amt, _ = _get_amount(o)
            if amt:
                prov_total += amt

        overdue_tag = ""
        if show_overdue and prov_orders:
            max_days = max(o.get("_days_overdue", 0) for o in prov_orders)
            overdue_tag = f" (\u0e40\u0e25\u0e22 {max_days} \u0e27\u0e31\u0e19)"  # (เลย N วัน)

        icon = "\U0001f534" if show_overdue else "\U0001f4cc"  # 🔴 or 📌
        lines.append(
            f"{icon} {prov_name} \u2014 {len(prov_orders)} "
            f"\u0e23\u0e32\u0e22\u0e01\u0e32\u0e23 "
            f"\u0e23\u0e27\u0e21 \u0e3f{_fmt_amount(prov_total)}{overdue_tag}"
        )

        for i, order in enumerate(prov_orders, 1):
            name = (order.get("Name") or "Unknown").strip()
            pax = _format_pax(order)
            pkg = (order.get("Type_of_Package") or "").strip()
            tour_date = _parse_date(order.get("Tour_Date"))
            tour_str = _format_thai_date(tour_date)
            amt_val, amt_str = _get_amount(order)
            order_id = order.get("id", "")

            dup_flag = ""
            if order_id in duplicates:
                dup_flag = " \u26a0\ufe0f \u0e15\u0e23\u0e27\u0e08\u0e2a\u0e2d\u0e1a \u2014 \u0e2d\u0e32\u0e08\u0e0b\u0e49\u0e33"
                # ⚠️ ตรวจสอบ — อาจซ้ำ

            lines.append(f"   {i}. {name} ({pax}) {pkg} {tour_str}")
            lines.append(f"      \u0e3f{amt_str}{dup_flag}")
            lines.append(_format_bank(provider))

        lines.append("")
        total_amount += prov_total
        total_count += len(prov_orders)

    return lines, total_amount, total_count


def _build_paid_section(orders, providers):
    """Build summary lines for orders paid yesterday."""
    grouped = {}
    for order in orders:
        _, prov_name = _get_provider_info(order)
        prov_name = prov_name or "Unknown Provider"
        grouped.setdefault(prov_name, []).append(order)

    lines = []
    total_amount = 0

    for prov_name in sorted(grouped.keys()):
        prov_orders = grouped[prov_name]
        prov_total = 0
        for o in prov_orders:
            amt, _ = _get_amount(o)
            if amt:
                prov_total += amt

        mod_time = prov_orders[0].get("Modified_Time")
        paid_date = _parse_date(mod_time)
        paid_str = _format_thai_date(paid_date) if paid_date else "?"

        lines.append(
            f"\u2705 {prov_name} \u2014 {len(prov_orders)} "
            f"\u0e23\u0e32\u0e22\u0e01\u0e32\u0e23 "
            f"\u0e3f{_fmt_amount(prov_total)} "
            f"(\u0e08\u0e48\u0e32\u0e22 {paid_str})"
        )
        total_amount += prov_total

    return lines, total_amount


def build_report(due_today, overdue, paid_yesterday, providers, today, duplicates):
    """Build the full LINE message."""
    lines = []

    # --- DUE TODAY ---
    if due_today:
        lines.append(f"\U0001f4b0 \u0e04\u0e23\u0e1a\u0e01\u0e33\u0e2b\u0e19\u0e14\u0e08\u0e48\u0e32\u0e22 Provider \u2014 {_format_thai_date_full(today)}")
        lines.append("")
        section, due_total, due_count = _build_provider_section(
            due_today, providers, duplicates
        )
        lines.extend(section)

        prov_count = len({_get_provider_info(o)[0] for o in due_today})
        lines.append(
            f"\U0001f4b0 \u0e23\u0e27\u0e21\u0e27\u0e31\u0e19\u0e19\u0e35\u0e49: "
            f"\u0e3f{_fmt_amount(due_total)} "
            f"({due_count} \u0e23\u0e32\u0e22\u0e01\u0e32\u0e23, "
            f"{prov_count} providers)"
        )
    else:
        lines.append(
            f"\u2705 \u0e44\u0e21\u0e48\u0e21\u0e35 Provider "
            f"\u0e17\u0e35\u0e48\u0e04\u0e23\u0e1a\u0e01\u0e33\u0e2b\u0e19\u0e14\u0e08\u0e48\u0e32\u0e22\u0e27\u0e31\u0e19\u0e19\u0e35\u0e49 "
            f"\u2014 {_format_thai_date_full(today)}"
        )
        due_total = 0

    # --- OVERDUE ---
    if overdue:
        lines.append("")
        lines.append(
            "\u2501\u2501\u2501\u2501\u2501 "
            "\u0e04\u0e49\u0e32\u0e07\u0e08\u0e48\u0e32\u0e22 (OVERDUE) "
            "\u2501\u2501\u2501\u2501\u2501"
        )
        lines.append("")
        section, overdue_total, overdue_count = _build_provider_section(
            overdue, providers, duplicates, show_overdue=True
        )
        lines.extend(section)
        lines.append(
            f"\U0001f534 \u0e23\u0e27\u0e21\u0e04\u0e49\u0e32\u0e07\u0e08\u0e48\u0e32\u0e22: "
            f"\u0e3f{_fmt_amount(overdue_total)} "
            f"({overdue_count} \u0e23\u0e32\u0e22\u0e01\u0e32\u0e23)"
        )
    else:
        overdue_total = 0

    # --- PAID YESTERDAY ---
    if paid_yesterday:
        lines.append("")
        lines.append(
            "\u2501\u2501\u2501\u2501\u2501 "
            "\u0e08\u0e48\u0e32\u0e22\u0e41\u0e25\u0e49\u0e27\u0e40\u0e21\u0e37\u0e48\u0e2d\u0e27\u0e32\u0e19 "
            "\u2501\u2501\u2501\u2501\u2501"
        )
        lines.append("")
        paid_lines, paid_total = _build_paid_section(paid_yesterday, providers)
        lines.extend(paid_lines)
    else:
        paid_total = 0

    # --- SUMMARY ---
    lines.append("")
    lines.append("\u2501" * 17)
    lines.append("\U0001f4ca \u0e2a\u0e23\u0e38\u0e1b\u0e23\u0e27\u0e21:")
    lines.append(f"   \u0e27\u0e31\u0e19\u0e19\u0e35\u0e49\u0e15\u0e49\u0e2d\u0e07\u0e08\u0e48\u0e32\u0e22: \u0e3f{_fmt_amount(due_total)}")
    lines.append(f"   \u0e04\u0e49\u0e32\u0e07\u0e08\u0e48\u0e32\u0e22: \u0e3f{_fmt_amount(overdue_total)}")
    lines.append(
        f"   \u0e08\u0e48\u0e32\u0e22\u0e41\u0e25\u0e49\u0e27\u0e40\u0e21\u0e37\u0e48\u0e2d\u0e27\u0e32\u0e19: "
        f"\u0e3f{_fmt_amount(paid_total)}"
    )

    # --- FOOTER ---
    lines.append("")
    lines.append(
        "\u26a0\ufe0f \u0e08\u0e48\u0e32\u0e22\u0e41\u0e25\u0e49\u0e27 \u2192 "
        "\u0e2d\u0e31\u0e1e\u0e40\u0e14\u0e17 Provider Payment Status = \"Paid\""
    )
    lines.append(
        "   + \u0e43\u0e2a\u0e48 Bank Reference \u0e43\u0e19 Zoho \u0e17\u0e31\u0e19\u0e17\u0e35"
    )
    lines.append(
        "   (\u0e23\u0e30\u0e1a\u0e1a\u0e15\u0e23\u0e27\u0e08\u0e2a\u0e2d\u0e1a "
        "\u2014 \u0e44\u0e21\u0e48\u0e43\u0e2a\u0e48 Bank Reference "
        "\u0e08\u0e30 flag Disputed)"
    )

    return "\n".join(lines).strip()


# ---------------------------------------------------------------------------
# Main orchestrator
# ---------------------------------------------------------------------------

def run_payment_register():
    """Main entry point. Returns (message, stats)."""
    today = datetime.now(ICT).date()
    logger.info(f"[PAY-REG] Running for date: {today}")

    # Fetch data
    unpaid_orders = _fetch_unpaid_orders()
    paid_yesterday = _fetch_paid_yesterday()

    # Get unique provider IDs
    provider_ids = set()
    for order in unpaid_orders + paid_yesterday:
        prov_id, _ = _get_provider_info(order)
        if prov_id:
            provider_ids.add(prov_id)

    providers = _fetch_providers(provider_ids)

    # Classify
    due_today, overdue = _classify_orders(unpaid_orders, providers, today)

    # Detect duplicates
    duplicates = _detect_duplicates(due_today, providers)

    # Build message
    message = build_report(due_today, overdue, paid_yesterday, providers, today, duplicates)

    stats = {
        "date": str(today),
        "due_today": len(due_today),
        "due_today_amount": sum(_get_amount(o)[0] or 0 for o in due_today),
        "overdue": len(overdue),
        "overdue_amount": sum(_get_amount(o)[0] or 0 for o in overdue),
        "paid_yesterday": len(paid_yesterday),
        "paid_yesterday_amount": sum(_get_amount(o)[0] or 0 for o in paid_yesterday),
        "providers": len(providers),
    }

    logger.info(f"[PAY-REG] Stats: {stats}")
    return message, stats
