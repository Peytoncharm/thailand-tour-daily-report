import os
import logging
from datetime import datetime, timedelta, date
from calendar import monthrange
from zoneinfo import ZoneInfo

from zoho_thailand import zoho_get_records, zoho_search

logger = logging.getLogger(__name__)

ICT = ZoneInfo("Asia/Bangkok")

LINE_TOKEN = os.environ.get("PA_LINE_TOKEN", "")
LINE_GROUP = os.environ.get("MONTHLY_REPORT_LINE_GROUP_ID", "")


# ---------------------------------------------------------------------------
# Zoho queries
# ---------------------------------------------------------------------------

ORDER_FIELDS = (
    "Name,Tour_Date,Type_of_Package,Provider_List,"
    "Provider_Payment_Status,Net_Cost,Total_Net_Cost_Currency,"
    "Adults1,Children1,Chanel_of_booking,Created_Time,Modified_Time"
)

PROVIDER_FIELDS = (
    "Name,Payment_Trigger,Days_Offset,"
    "Bank_Details,Bank_Account_Number,Bank_Account_Name"
)


def _filter_unpaid(records, today):
    """Filter records to unpaid orders with providers, Tour_Date >= 60 days ago."""
    cutoff = today - timedelta(days=60)

    filtered = []
    for r in records:
        pl = r.get("Provider_List")
        if not pl or not isinstance(pl, dict):
            continue
        pps = (r.get("Provider_Payment_Status") or "").strip()
        if pps == "Paid":
            continue
        tour_date = _parse_date(r.get("Tour_Date"))
        if not tour_date or tour_date < cutoff:
            continue
        if (r.get("Chanel_of_booking") or "").upper() == "TEST":
            continue
        filtered.append(r)

    logger.info(f"[PAY-REG] Unpaid orders: {len(filtered)} after filters")
    return filtered


def _filter_paid_yesterday(records, today):
    """Filter records to orders marked Paid that were modified yesterday (ICT)."""
    yesterday_str = (today - timedelta(days=1)).strftime("%Y-%m-%d")

    filtered = []
    for r in records:
        pps = (r.get("Provider_Payment_Status") or "").strip()
        if pps != "Paid":
            continue
        pl = r.get("Provider_List")
        if not pl or not isinstance(pl, dict):
            continue
        mod_time = r.get("Modified_Time") or ""
        if not mod_time.startswith(yesterday_str):
            continue
        if (r.get("Chanel_of_booking") or "").upper() == "TEST":
            continue
        filtered.append(r)

    logger.info(f"[PAY-REG] Paid yesterday: {len(filtered)} after filters")
    return filtered


# ---------------------------------------------------------------------------
# Provider lookup
# ---------------------------------------------------------------------------

def _fetch_providers(provider_ids):
    """Fetch provider records by name lookup. Returns dict keyed by provider ID."""
    if not provider_ids:
        return {}

    # We have IDs but zoho_search works by name. Collect names from orders
    # instead — caller should pass provider_names_map.
    # Fallback: fetch all providers and match by ID.
    all_providers = zoho_get_records("Providers", fields=PROVIDER_FIELDS)
    providers = {}
    for r in all_providers:
        pid = r.get("id", "")
        if pid in provider_ids:
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
    """Format number as whole Thai baht string (no decimals)."""
    if val is None:
        return "0"
    try:
        return f"{round(float(val)):,}"
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
        return None

    parts = []
    if bank:
        parts.append(bank)
    if acct_num:
        parts.append(acct_num)
    line = f"   🏦 {' '.join(parts)}"
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
            overdue_tag = f" (เลย {max_days} วัน)"

        icon = "🔴" if show_overdue else "📌"
        lines.append(
            f"{icon} {prov_name} — {len(prov_orders)} รายการ "
            f"฿{_fmt_amount(prov_total)}{overdue_tag}"
        )

        # Bank details once per provider
        bank_line = _format_bank(provider)
        if bank_line:
            lines.append(bank_line)

        for i, order in enumerate(prov_orders, 1):
            name = (order.get("Name") or "Unknown").strip()
            pax = _format_pax(order)
            tour_date = _parse_date(order.get("Tour_Date"))
            tour_str = _format_thai_date(tour_date)
            _, amt_str = _get_amount(order)
            order_id = order.get("id", "")

            dup_flag = ""
            if order_id in duplicates:
                dup_flag = " ⚠️ซ้ำ?"

            lines.append(f"   {i}. {name} ({pax}) ฿{amt_str} — {tour_str}{dup_flag}")

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

        lines.append(
            f"✅ {prov_name} — {len(prov_orders)} รายการ "
            f"฿{_fmt_amount(prov_total)}"
        )
        total_amount += prov_total

    return lines, total_amount


def build_report(due_today, overdue, paid_yesterday, providers, today, duplicates):
    """Build the full LINE message."""
    lines = []

    # --- DUE TODAY ---
    if due_today:
        lines.append(f"💰 ครบกำหนดจ่าย — {_format_thai_date_full(today)}")
        lines.append("")
        section, due_total, due_count = _build_provider_section(
            due_today, providers, duplicates
        )
        lines.extend(section)
        lines.append(
            f"💰 รวมวันนี้: ฿{_fmt_amount(due_total)} ({due_count} รายการ)"
        )
    else:
        lines.append(
            f"✅ ไม่มี Provider ที่ครบกำหนดจ่ายวันนี้ — {_format_thai_date_full(today)}"
        )
        due_total = 0

    # --- OVERDUE ---
    if overdue:
        lines.append("")
        lines.append("━━━ 🔴 OVERDUE ━━━")
        lines.append("")
        section, overdue_total, overdue_count = _build_provider_section(
            overdue, providers, duplicates, show_overdue=True
        )
        lines.extend(section)
        lines.append(
            f"🔴 รวมค้างจ่าย: ฿{_fmt_amount(overdue_total)} ({overdue_count} รายการ)"
        )
    else:
        overdue_total = 0

    # --- PAID YESTERDAY ---
    if paid_yesterday:
        lines.append("")
        lines.append("━━━ ✅ PAID YESTERDAY ━━━")
        lines.append("")
        paid_lines, paid_total = _build_paid_section(paid_yesterday, providers)
        lines.extend(paid_lines)
    else:
        paid_total = 0

    # --- SUMMARY ---
    lines.append("")
    lines.append("━━━━━━━━━━━━━━━━━")
    lines.append("สรุป:")
    lines.append(f"  วันนี้ต้องจ่าย: ฿{_fmt_amount(due_total)}")
    lines.append(f"  ค้างจ่าย: ฿{_fmt_amount(overdue_total)}")
    lines.append(f"  จ่ายแล้วเมื่อวาน: ฿{_fmt_amount(paid_total)}")

    return "\n".join(lines).strip()


# ---------------------------------------------------------------------------
# Main orchestrator
# ---------------------------------------------------------------------------

def run_payment_register():
    """Main entry point. Returns (message, stats)."""
    today = datetime.now(ICT).date()
    logger.info(f"[PAY-REG] Running for date: {today}")

    # Fetch all orders once, then split
    all_orders = zoho_get_records("Koh_Chang_Orders", fields=ORDER_FIELDS)
    logger.info(f"[PAY-REG] Fetched {len(all_orders)} total Koh_Chang_Orders")
    unpaid_orders = _filter_unpaid(all_orders, today)
    paid_yesterday = _filter_paid_yesterday(all_orders, today)

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
    duplicates = set()

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
