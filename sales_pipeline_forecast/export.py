"""Export helpers: a multi-sheet Excel workbook, plain CSV, and a
Quicken Simplifi-compatible CSV (Date, Payee, Amount, Tags — the column set
Simplifi's manual CSV importer expects, dates as M/D/YYYY).
"""
import io

import pandas as pd


def _simplifi_date(d) -> str:
    # %-m/%-d isn't portable to all platforms (e.g. Windows); build it by hand.
    return f"{d.month}/{d.day}/{d.year}"


def deals_dataframe(deals: list) -> pd.DataFrame:
    df = pd.DataFrame(deals)
    if df.empty:
        return pd.DataFrame(columns=[
            "id", "name", "account", "amount", "stage", "probability",
            "forecast_category", "expected_close_date", "created_date", "notes",
        ])
    return df[[
        "id", "name", "account", "amount", "stage", "probability",
        "forecast_category", "expected_close_date", "created_date", "notes",
    ]]


def forecast_dataframe(forecast_rows: list) -> pd.DataFrame:
    df = pd.DataFrame(forecast_rows)
    if df.empty:
        return df
    return df[[
        "period_label", "quota", "bookings", "attainment_pct",
        "base", "commission", "total_earnings", "deal_count",
    ]].rename(columns={
        "period_label": "Period", "quota": "Quota", "bookings": "Bookings",
        "attainment_pct": "Attainment %", "base": "Base", "commission": "Commission",
        "total_earnings": "Total Earnings", "deal_count": "# Deals",
    })


def comp_plan_dataframe(comp_plan: dict) -> pd.DataFrame:
    rows = [
        {"Setting": "Annual Quota", "Value": comp_plan["annual_quota"]},
        {"Setting": "Annual Base Salary", "Value": comp_plan["annual_base_salary"]},
        {"Setting": "Quota Period", "Value": comp_plan["quota_period"]},
    ]
    for tier in comp_plan["commission_tiers"]:
        cap = tier["up_to_pct"]
        label = f"Commission tier up to {cap}% attainment" if cap is not None else "Commission tier beyond last cap"
        rows.append({"Setting": label, "Value": f"{tier['rate_pct']}%"})
    return pd.DataFrame(rows)


def export_excel_workbook(deals: list, forecast_rows: list, comp_plan: dict) -> bytes:
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        deals_dataframe(deals).to_excel(writer, sheet_name="Pipeline", index=False)
        forecast_dataframe(forecast_rows).to_excel(writer, sheet_name="Forecast", index=False)
        comp_plan_dataframe(comp_plan).to_excel(writer, sheet_name="Comp Plan", index=False)
    return buffer.getvalue()


def export_pipeline_csv(deals: list) -> bytes:
    return deals_dataframe(deals).to_csv(index=False).encode("utf-8")


def export_forecast_csv(forecast_rows: list) -> bytes:
    return forecast_dataframe(forecast_rows).to_csv(index=False).encode("utf-8")


def export_simplifi_deals_csv(deals: list, scenario_amounts: dict) -> bytes:
    """One row per open/won deal, as a projected-income transaction.

    ``scenario_amounts`` maps deal id -> the dollar amount to record for the
    currently selected scenario (so a probability-weighted deal shows its
    weighted value, not its full contract value).
    """
    rows = []
    for deal in deals:
        if deal["forecast_category"] in ("Closed Lost", "Omitted"):
            continue
        amount = scenario_amounts.get(deal["id"], 0.0)
        if amount <= 0:
            continue
        from datetime import date as _date
        close_date = _date.fromisoformat(str(deal["expected_close_date"])[:10])
        payee = f"{deal['account']} - {deal['name']}" if deal["account"] else deal["name"]
        rows.append({
            "Date": _simplifi_date(close_date),
            "Payee": payee,
            "Amount": round(amount, 2),
            "Tags": f"Sales Pipeline:{deal['forecast_category']}",
        })
    return pd.DataFrame(rows, columns=["Date", "Payee", "Amount", "Tags"]).to_csv(index=False).encode("utf-8")


def export_simplifi_earnings_csv(forecast_rows: list) -> bytes:
    """One row per forecast period: projected total earnings as income."""
    from forecast import period_bounds

    rows = []
    for row in forecast_rows:
        period_start, _ = period_bounds(row["period"], "monthly" if "-Q" not in row["period"] else "quarterly")
        rows.append({
            "Date": _simplifi_date(period_start),
            "Payee": f"Projected Earnings - {row['period_label']}",
            "Amount": round(row["total_earnings"], 2),
            "Tags": "Commission Forecast",
        })
    return pd.DataFrame(rows, columns=["Date", "Payee", "Amount", "Tags"]).to_csv(index=False).encode("utf-8")
