import os
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from zoho_thailand import zoho_get_records

logger = logging.getLogger(__name__)

ICT = ZoneInfo("Asia/Bangkok")

LINE_TOKEN = os.environ.get("PA_LINE_TOKEN", "")
LINE_GROUP = os.environ.get("MONTHLY_REPORT_LINE_GROUP_ID", "")

TOUR_TYPES = {"Individual Activity", "Package Activity"}
TRANSFER_TYPES = {"Join Transfer", "Private Transfer"}

ORDER_FIELDS = (
    "Name,Tour_Date,Type_of_Package,Total_Amount,Net_Cost,"
    "Total_Profit_Cost,OMISE_Fee,Payment_Method,Provider_List,"
    "Chanel_of_booking,Extra_Charge"
)

THAI_MONTHS = {
    1: "\u0e21.\u0e04.", 2: "\u0e01.\u0e1e.", 3: "\u0e21\u0e35.\u0e04.",
    4: "\u0e40\u0e21.\u0e22.", 5: "\u0e1e.\u0e04.", 6: "\u0e21\u0e34.\u0e22.",
    7: "\u0e01.\u0e04.", 8: "\u0e2a.\u0e04.", 9: "\u0e01.\u0e22.",
    10: "\u0e15.\u0e04.", 11: "\u0e1e.\u0e22.", 12: "\u0e18.\u0e04.",
}


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------

def _get_report_period(month_str=None):
    """Return (start_date, end_date, display_str) for the report period.
    month_str format: 'YYYY-MM' (e.g. '2026-04'). If None, uses previous month.
    """
    if month_str:
        try:
            year, month = int(month_str[:4]), int(month_str[5:7])
            start = datetime(year, month, 1).date()
        except (ValueError, IndexError):
            logger.error(f"[MONTHLY] Invalid month_str: {month_str}")
            return None, None, None
    else:
        today = datetime.now(ICT).date()
        first_of_this_month = today.replace(day=1)
        last_month_end = first_of_this_month - timedelta(days=1)
        start = last_month_end.replace(day=1)

    # End of month
    if start.month == 12:
        end = start.replace(year=start.year + 1, month=1, day=1) - timedelta(days=1)
    else:
        end = start.replace(month=start.month + 1, day=1) - timedelta(days=1)

    display = f"{THAI_MONTHS.get(start.month, '')} {start.year}"
    return start, end, display


def _parse_date(val):
    """Parse date string to date object, or None."""
    if not val:
        return None
    try:
        raw = val.split("T")[0] if "T" in val else val
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Data fetch + filter
# ---------------------------------------------------------------------------

def _fetch_and_filter(start_date, end_date):
    """Fetch all orders, filter to date range and exclude TEST."""
    records = zoho_get_records("Koh_Chang_Orders", fields=ORDER_FIELDS)
    logger.info(f"[MONTHLY] Fetched {len(records)} total records")

    filtered = []
    for r in records:
        tour_date = _parse_date(r.get("Tour_Date"))
        if not tour_date:
            continue
        if tour_date < start_date or tour_date > end_date:
            continue
        if (r.get("Chanel_of_booking") or "").upper() == "TEST":
            continue
        filtered.append(r)

    logger.info(f"[MONTHLY] Filtered to {len(filtered)} orders for {start_date} to {end_date}")
    return filtered


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_float(val):
    """Convert value to float, defaulting to 0.0."""
    if val is None:
        return 0.0
    try:
        return float(val)
    except (ValueError, TypeError):
        return 0.0


def _fmt_amount(val):
    """Format number as whole Thai baht string (no decimals)."""
    if val is None:
        return "0"
    try:
        return f"{round(float(val)):,}"
    except (ValueError, TypeError):
        return "0"


def _classify_by_bu(orders):
    """Split orders into tour and transfer lists."""
    tour = []
    transfer = []
    for o in orders:
        pkg = (o.get("Type_of_Package") or "").strip()
        if pkg in TOUR_TYPES:
            tour.append(o)
        elif pkg in TRANSFER_TYPES:
            transfer.append(o)
    return tour, transfer


# ---------------------------------------------------------------------------
# Stats computation
# ---------------------------------------------------------------------------

def _compute_bu_stats(orders):
    """Compute financial stats for a set of orders."""
    bookings = len(orders)
    income = sum(_safe_float(o.get("Total_Amount")) for o in orders)
    cost = sum(_safe_float(o.get("Net_Cost")) for o in orders)
    gross_profit = sum(_safe_float(o.get("Total_Profit_Cost")) for o in orders)
    omise_fees = sum(_safe_float(o.get("OMISE_Fee")) for o in orders)
    net_profit = gross_profit - omise_fees
    margin = (gross_profit / income * 100) if income > 0 else 0.0

    # Payment method breakdown
    payment = {"Cash": [], "Credit Card": [], "Mobile Banking": [], "Other": []}
    for o in orders:
        method = (o.get("Payment_Method") or "").strip()
        amt = _safe_float(o.get("Total_Amount"))
        if method == "Cash":
            payment["Cash"].append(amt)
        elif method == "Credit Card":
            payment["Credit Card"].append(amt)
        elif method == "Mobile Banking":
            payment["Mobile Banking"].append(amt)
        else:
            payment["Other"].append(amt)

    pay_stats = {}
    for method, amounts in payment.items():
        pay_stats[method] = {
            "count": len(amounts),
            "total": sum(amounts),
        }

    return {
        "bookings": bookings,
        "income": income,
        "cost": cost,
        "gross_profit": gross_profit,
        "omise_fees": omise_fees,
        "net_profit": net_profit,
        "margin": margin,
        "payment": pay_stats,
    }


def _compute_top_providers(orders, n=10):
    """Group orders by provider, return top N by cost."""
    providers = {}
    for o in orders:
        pl = o.get("Provider_List")
        if not isinstance(pl, dict):
            continue
        prov_name = (pl.get("name") or "").strip()
        if not prov_name:
            continue
        pkg = (o.get("Type_of_Package") or "").strip()

        if prov_name not in providers:
            providers[prov_name] = {
                "cost": 0, "income": 0, "profit": 0, "bookings": 0,
                "types": [],
            }
        p = providers[prov_name]
        p["cost"] += _safe_float(o.get("Net_Cost"))
        p["income"] += _safe_float(o.get("Total_Amount"))
        p["profit"] += _safe_float(o.get("Total_Profit_Cost"))
        p["bookings"] += 1
        p["types"].append(pkg)

    # Determine BU icon per provider (most common type)
    result = []
    for name, data in providers.items():
        tour_count = sum(1 for t in data["types"] if t in TOUR_TYPES)
        transfer_count = sum(1 for t in data["types"] if t in TRANSFER_TYPES)
        icon = "\U0001f3dd\ufe0f" if tour_count >= transfer_count else "\U0001f690"
        result.append({
            "name": name,
            "icon": icon,
            "cost": data["cost"],
            "income": data["income"],
            "profit": data["profit"],
            "bookings": data["bookings"],
        })

    result.sort(key=lambda x: x["cost"], reverse=True)
    return result[:n]


# ---------------------------------------------------------------------------
# Message formatting
# ---------------------------------------------------------------------------

def _build_bu_section(label, icon, stats):
    """Build message lines for one BU section."""
    lines = [
        f"━━━ {icon} {label} ━━━",
        f"Bookings: {stats['bookings']}",
        "",
        f"Income:     ฿{_fmt_amount(stats['income'])}",
    ]

    pay = stats["payment"]
    if pay["Cash"]["count"] > 0:
        lines.append(f"  Cash:     ฿{_fmt_amount(pay['Cash']['total'])} ({pay['Cash']['count']})")
    if pay["Credit Card"]["count"] > 0:
        lines.append(f"  Card:     ฿{_fmt_amount(pay['Credit Card']['total'])} ({pay['Credit Card']['count']})")
    if pay["Mobile Banking"]["count"] > 0:
        lines.append(f"  Mobile:   ฿{_fmt_amount(pay['Mobile Banking']['total'])} ({pay['Mobile Banking']['count']})")
    if pay["Other"]["count"] > 0:
        lines.append(f"  Other:    ฿{_fmt_amount(pay['Other']['total'])} ({pay['Other']['count']})")

    lines.extend([
        "",
        f"Cost:       ฿{_fmt_amount(stats['cost'])}",
        f"Profit:     ฿{_fmt_amount(stats['net_profit'])}",
        f"OMISE:      ฿{_fmt_amount(stats['omise_fees'])}",
        f"Margin:     {stats['margin']:.1f}%",
    ])
    return lines


def _build_combined_section(tour_stats, transfer_stats):
    """Build combined totals section."""
    total_bookings = tour_stats["bookings"] + transfer_stats["bookings"]
    total_income = tour_stats["income"] + transfer_stats["income"]
    total_cost = tour_stats["cost"] + transfer_stats["cost"]
    total_gross = tour_stats["gross_profit"] + transfer_stats["gross_profit"]
    total_omise = tour_stats["omise_fees"] + transfer_stats["omise_fees"]
    total_net = total_gross - total_omise
    total_margin = (total_gross / total_income * 100) if total_income > 0 else 0.0

    # Combine payment methods
    combined_pay = {}
    for method in ["Cash", "Credit Card", "Mobile Banking", "Other"]:
        count = tour_stats["payment"][method]["count"] + transfer_stats["payment"][method]["count"]
        total = tour_stats["payment"][method]["total"] + transfer_stats["payment"][method]["total"]
        combined_pay[method] = {"count": count, "total": total}

    lines = [
        "━━━ 📊 TOTAL ━━━",
        f"Bookings: {total_bookings}",
        f"Income:     ฿{_fmt_amount(total_income)}",
        f"Cost:       ฿{_fmt_amount(total_cost)}",
        f"Net Profit: ฿{_fmt_amount(total_net)}",
        f"Margin:     {total_margin:.1f}%",
        "",
    ]

    # Payment mix summary line
    pay_parts = []
    for method, label in [("Cash", "Cash"), ("Credit Card", "Card"), ("Mobile Banking", "Mobile"), ("Other", "Other")]:
        pay = combined_pay[method]
        if pay["count"] > 0:
            pct = (pay["total"] / total_income * 100) if total_income > 0 else 0
            pay_parts.append(f"{label} {pct:.0f}%")

    if pay_parts:
        lines.append(" | ".join(pay_parts))

    return lines


def _build_top_providers_section(top_providers):
    """Build top providers section."""
    lines = [
        "",
        "━━━ 🏆 TOP PROVIDERS ━━━",
    ]
    for i, p in enumerate(top_providers, 1):
        lines.append(
            f"{i}. {p['name']} {p['icon']} — ฿{_fmt_amount(p['cost'])} ({p['bookings']})"
        )
    return lines


# ---------------------------------------------------------------------------
# Main orchestrator
# ---------------------------------------------------------------------------

def build_monthly_report(month_str=None):
    """Main entry point. Returns (message, stats)."""
    start_date, end_date, display = _get_report_period(month_str)
    if start_date is None:
        return "Error: invalid month", {"error": "invalid month"}

    logger.info(f"[MONTHLY] Building report for {display} ({start_date} to {end_date})")

    orders = _fetch_and_filter(start_date, end_date)

    if not orders:
        msg = f"\U0001f4ca Monthly P&L Report \u2014 {display}\n\n\u0e44\u0e21\u0e48\u0e21\u0e35\u0e02\u0e49\u0e2d\u0e21\u0e39\u0e25"
        return msg, {"period": display, "bookings": 0}

    tour_orders, transfer_orders = _classify_by_bu(orders)
    tour_stats = _compute_bu_stats(tour_orders)
    transfer_stats = _compute_bu_stats(transfer_orders)
    top_providers = _compute_top_providers(orders)

    # Build message
    lines = [
        f"📊 Monthly P&L — {display}",
        "",
    ]

    if tour_orders:
        lines.extend(_build_bu_section("TOUR (Activity)", "🏝️", tour_stats))
        lines.append("")
        lines.append("")

    if transfer_orders:
        lines.extend(_build_bu_section("TRANSFER", "🚐", transfer_stats))
        lines.append("")
        lines.append("")

    lines.extend(_build_combined_section(tour_stats, transfer_stats))

    if top_providers:
        lines.extend(_build_top_providers_section(top_providers))

    message = "\n".join(lines).strip()

    stats = {
        "period": display,
        "start_date": str(start_date),
        "end_date": str(end_date),
        "total_bookings": len(orders),
        "tour_bookings": tour_stats["bookings"],
        "tour_income": tour_stats["income"],
        "tour_cost": tour_stats["cost"],
        "tour_profit": tour_stats["gross_profit"],
        "transfer_bookings": transfer_stats["bookings"],
        "transfer_income": transfer_stats["income"],
        "transfer_cost": transfer_stats["cost"],
        "transfer_profit": transfer_stats["gross_profit"],
        "total_income": tour_stats["income"] + transfer_stats["income"],
        "total_net_profit": (tour_stats["gross_profit"] + transfer_stats["gross_profit"])
                          - (tour_stats["omise_fees"] + transfer_stats["omise_fees"]),
        "overall_margin": ((tour_stats["gross_profit"] + transfer_stats["gross_profit"])
                          / (tour_stats["income"] + transfer_stats["income"]) * 100)
                          if (tour_stats["income"] + transfer_stats["income"]) > 0 else 0,
    }

    logger.info(f"[MONTHLY] Report built: {stats['total_bookings']} bookings, margin {stats['overall_margin']:.1f}%")
    return message, stats
