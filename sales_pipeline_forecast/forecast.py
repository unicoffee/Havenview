"""Pipeline weighting, period bucketing, and tiered commission math.

No I/O here — pure functions over deal dicts and a comp-plan dict, so the
logic is easy to unit-test independently of Streamlit/SQLite.
"""
import math
from datetime import date, timedelta

SCENARIOS = ["weighted", "best_case", "commit"]
SCENARIO_LABELS = {
    "weighted": "Weighted pipeline (probability x amount)",
    "best_case": "Best case (Best Case + Commit, full value)",
    "commit": "Commit (Commit only, full value)",
}


def parse_date(value):
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def period_key(d: date, period_type: str) -> str:
    if period_type == "monthly":
        return f"{d.year}-{d.month:02d}"
    quarter = (d.month - 1) // 3 + 1
    return f"{d.year}-Q{quarter}"


def period_bounds(key: str, period_type: str):
    if period_type == "monthly":
        year, month = (int(x) for x in key.split("-"))
        start = date(year, month, 1)
        end = date(year + (month == 12), (month % 12) + 1, 1) - timedelta(days=1)
    else:
        year, q = key.split("-Q")
        year, q = int(year), int(q)
        start_month = (q - 1) * 3 + 1
        start = date(year, start_month, 1)
        # First day of the month after the quarter, minus one day.
        next_month = start_month + 3
        next_year = year + (next_month - 1) // 12
        next_month = ((next_month - 1) % 12) + 1
        end = date(next_year, next_month, 1) - timedelta(days=1)
    return start, end


def period_label(key: str, period_type: str) -> str:
    if period_type == "monthly":
        year, month = (int(x) for x in key.split("-"))
        return date(year, month, 1).strftime("%b %Y")
    return key


def generate_periods(start: date, num_periods: int, period_type: str):
    """Return an ordered list of period keys starting from ``start``'s period."""
    periods = []
    key = period_key(start, period_type)
    cursor = period_bounds(key, period_type)[0]
    for _ in range(num_periods):
        k = period_key(cursor, period_type)
        periods.append(k)
        _, end = period_bounds(k, period_type)
        cursor = end + timedelta(days=1)
    return periods


def periods_per_year(period_type: str) -> int:
    return 12 if period_type == "monthly" else 4


def scenario_deal_amount(deal: dict, scenario: str) -> float:
    """Dollars this deal contributes to bookings under ``scenario``.

    Closed Won always counts at full value in every scenario (it's realized,
    not projected). Closed Lost / Omitted never count. Open deals count
    according to the scenario's forecast-category filter.
    """
    fc = deal["forecast_category"]
    amount = deal["amount"]
    if fc == "Closed Won":
        return amount
    if fc in ("Closed Lost", "Omitted"):
        return 0.0
    if scenario == "weighted":
        return amount * (deal["probability"] / 100.0)
    if scenario == "best_case":
        return amount if fc in ("Best Case", "Commit") else 0.0
    if scenario == "commit":
        return amount if fc == "Commit" else 0.0
    raise ValueError(f"Unknown scenario: {scenario}")


def compute_commission(bookings: float, quota: float, tiers: list):
    """Tiered/accelerated commission on ``bookings`` against a period ``quota``.

    ``tiers`` is a list of ``{"up_to_pct": float|None, "rate_pct": float}``,
    sorted ascending by ``up_to_pct`` (``None`` = uncapped, must be last).
    Each tier's rate applies only to the slice of bookings that falls in
    that tier's quota-attainment band.
    """
    attainment_pct = (bookings / quota * 100.0) if quota else 0.0
    if quota <= 0:
        return 0.0, attainment_pct

    commission = 0.0
    floor_pct = 0.0
    for tier in tiers:
        cap_pct = tier["up_to_pct"]
        cap_amount = quota * cap_pct / 100.0 if cap_pct is not None else math.inf
        floor_amount = quota * floor_pct / 100.0
        slice_amount = max(0.0, min(bookings, cap_amount) - floor_amount)
        commission += slice_amount * tier["rate_pct"] / 100.0
        if cap_pct is None or bookings <= cap_amount:
            break
        floor_pct = cap_pct
    return commission, attainment_pct


def bucket_deals_by_period(deals: list, period_type: str):
    buckets = {}
    for deal in deals:
        d = parse_date(deal["expected_close_date"])
        key = period_key(d, period_type)
        buckets.setdefault(key, []).append(deal)
    return buckets


def build_forecast_table(deals: list, comp_plan: dict, scenario: str,
                          num_periods: int = 8, start: date = None):
    """Return a list of per-period forecast rows (dicts), oldest first."""
    period_type = comp_plan.get("quota_period", "quarterly")
    start = start or date.today()
    periods = generate_periods(start, num_periods, period_type)
    buckets = bucket_deals_by_period(deals, period_type)

    quota_per_period = comp_plan["annual_quota"] / periods_per_year(period_type)
    base_per_period = comp_plan["annual_base_salary"] / periods_per_year(period_type)
    tiers = comp_plan["commission_tiers"]

    rows = []
    for key in periods:
        period_deals = buckets.get(key, [])
        bookings = sum(scenario_deal_amount(d, scenario) for d in period_deals)
        commission, attainment_pct = compute_commission(bookings, quota_per_period, tiers)
        rows.append({
            "period": key,
            "period_label": period_label(key, period_type),
            "quota": quota_per_period,
            "bookings": bookings,
            "attainment_pct": attainment_pct,
            "base": base_per_period,
            "commission": commission,
            "total_earnings": base_per_period + commission,
            "deal_count": len(period_deals),
        })
    return rows


def attainment_status(attainment_pct: float) -> str:
    if attainment_pct >= 100:
        return "good"
    if attainment_pct >= 70:
        return "warning"
    return "critical"
