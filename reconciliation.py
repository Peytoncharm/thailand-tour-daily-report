import logging
from datetime import datetime, timezone, timedelta

from zoho_thailand import zoho_search

logger = logging.getLogger(__name__)

ICT = timezone(timedelta(hours=7))

REPORT_STATUSES = ["Awaiting Payment", "Quote Sent", "Booking Completed", "In Review"]

FIELDS = (
    "Name,Last_Name,Package,Status,Total_Amount,Net_Cost,"
    "Total_Profit_Cost,Payment_Method,Tour_Date,Number_of_People,"
    "Provider_Payment_Status,Modified_Time"
)

PAYMENT_ICONS = {
    "Cash": " \U0001f4b5",
    "Credit Card": " \U0001f4b3",
    "Bank Transfer": " \U0001f3e6",
}


def fetch_today_orders() -> list:
    """Fetch Koh Chang Orders modified today with target statuses."""
    status_csv = ",".join(REPORT_STATUSES)
    criteria = (
        f"((Modified_Time:equals:today)"
        f"and(Status:in:{status_csv}))"
    )
    logger.info(f"[RECON] Criteria: {criteria}")
    records = zoho_search("Koh_Chang_Orders", criteria, fields=FIELDS)
    logger.info(f"[RECON] Found {len(records)} orders modified today")
    return records


def _fmt_amount(val) -> str:
    if val is None:
        return "-"
    try:
        n = float(val)
        if n == int(n):
            return f"{int(n):,}"
        return f"{n:,.2f}"
    except (ValueError, TypeError):
        return "-"


def _short_name(record: dict) -> str:
    first = (record.get("Name") or "").strip()
    last = (record.get("Last_Name") or "").strip()
    if first and last and last != "-":
        return first
    return first or last or "Unknown"


def _short_package(record: dict) -> str:
    pkg = record.get("Package") or ""
    if len(pkg) > 35:
        pkg = pkg[:32] + "..."
    return pkg or "No package"


def build_report(records: list) -> str:
    """Build the LINE message from fetched records."""
    now = datetime.now(ICT)
    date_str = now.strftime("%d %b %Y (%a)")
    time_str = now.strftime("%H:%M")

    # Count by status
    counts = {s: 0 for s in REPORT_STATUSES}
    for r in records:
        st = r.get("Status", "")
        if st in counts:
            counts[st] += 1

    # Grand total amount
    grand_total = 0
    for r in records:
        amt = r.get("Total_Amount")
        if amt is not None:
            try:
                grand_total += float(amt)
            except (ValueError, TypeError):
                pass

    # Profit total
    profit_total = 0
    profit_count = 0
    for r in records:
        p = r.get("Total_Profit_Cost")
        if p is not None:
            try:
                profit_total += float(p)
                profit_count += 1
            except (ValueError, TypeError):
                pass

    # Unpaid to providers
    unpaid = sum(
        1 for r in records
        if not r.get("Provider_Payment_Status")
        or r.get("Provider_Payment_Status") in ("No", "Pending", None)
    )

    # Build message
    lines = [
        "\U0001f4ca Koh Chang Reconciliation",
        f"\U0001f4c5 {date_str} \u2014 {time_str} ICT",
        "",
    ]

    status_icons = {
        "Awaiting Payment": "\u23f3",
        "Quote Sent": "\U0001f4e8",
        "Booking Completed": "\u2705",
        "In Review": "\U0001f50d",
    }
    for status in REPORT_STATUSES:
        icon = status_icons.get(status, "\u2022")
        label = "Completed" if status == "Booking Completed" else status
        lines.append(f"{icon} {label}: {counts[status]}")

    lines.append("\u2501" * 18)
    lines.append(f"Total: {len(records)} orders | \u0e3f{_fmt_amount(grand_total)}")
    lines.append("")

    # Detail sections for actionable statuses
    for status in REPORT_STATUSES:
        status_records = [r for r in records if r.get("Status") == status]
        if not status_records:
            continue

        icon = status_icons.get(status, "\u2022")
        label = "Completed" if status == "Booking Completed" else status
        lines.append(f"{icon} {label} ({len(status_records)}):")

        if status == "Booking Completed" and len(status_records) > 5:
            # Aggregate for completed bookings if many
            total = sum(float(r.get("Total_Amount") or 0) for r in status_records)
            lines.append(f" \u2022 {len(status_records)} bookings \u2014 \u0e3f{_fmt_amount(total)} total")
        else:
            for r in status_records:
                name = _short_name(r)
                pkg = _short_package(r)
                amt = _fmt_amount(r.get("Total_Amount"))
                pay_icon = PAYMENT_ICONS.get(r.get("Payment_Method", ""), "")
                lines.append(f" \u2022 {name} \u2014 {pkg} \u2014 \u0e3f{amt}{pay_icon}")

        lines.append("")

    # Footer
    if profit_count > 0:
        lines.append(f"\U0001f4b0 Profit today: \u0e3f{_fmt_amount(profit_total)}")
    if unpaid > 0:
        lines.append(f"\U0001f4cb Unpaid to providers: {unpaid}")

    return "\n".join(lines).strip()


def build_empty_report() -> str:
    """Report when no orders were modified today."""
    now = datetime.now(ICT)
    date_str = now.strftime("%d %b %Y (%a)")
    return (
        f"\U0001f4ca Koh Chang Reconciliation\n"
        f"\U0001f4c5 {date_str} \u2014 18:00 ICT\n\n"
        f"\u2705 No orders modified today.\n"
        f"All clear!"
    )
