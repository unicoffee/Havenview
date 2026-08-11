"""Sales Pipeline & Earnings Forecast — a single Streamlit app for a
quota-carrying rep to track pipeline, forecast bookings and commission
earnings, and export the results to Excel / Quicken Simplifi / plain CSV.

Run with:  streamlit run app.py
"""
from datetime import date

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

import colors
import db
import export
import forecast

st.set_page_config(page_title="Sales Pipeline & Earnings Forecast", layout="wide")

db.init_db()
db.seed_if_empty()


def money(value: float) -> str:
    return f"${value:,.0f}"


# ---------------------------------------------------------------------------
# Sidebar — forecast controls shared across every tab
# ---------------------------------------------------------------------------
st.sidebar.title("Forecast settings")
comp_plan = db.get_comp_plan()

scenario = st.sidebar.selectbox(
    "Scenario",
    options=forecast.SCENARIOS,
    format_func=lambda s: forecast.SCENARIO_LABELS[s],
)
period_type_label = "quarters" if comp_plan["quota_period"] == "quarterly" else "months"
num_periods = st.sidebar.slider(f"Number of {period_type_label} to project", 1, 8, 4)

st.sidebar.caption(
    "The quota period (monthly vs. quarterly) is set on the **Comp Plan** tab — "
    "it controls how quota and base salary are split across periods."
)

deals = db.list_deals()
forecast_rows = forecast.build_forecast_table(
    deals, comp_plan, scenario, num_periods=num_periods, start=date.today()
)

st.title("Sales Pipeline & Earnings Forecast")

tab_pipeline, tab_forecast, tab_earnings, tab_comp_plan, tab_export = st.tabs(
    ["Pipeline", "Forecast", "Earnings", "Comp Plan", "Export"]
)

# ---------------------------------------------------------------------------
# Pipeline tab
# ---------------------------------------------------------------------------
with tab_pipeline:
    st.subheader("Add a deal")
    with st.form("add_deal_form", clear_on_submit=True):
        c1, c2, c3 = st.columns(3)
        name = c1.text_input("Deal name")
        account = c2.text_input("Account")
        amount = c3.number_input("Amount ($)", min_value=0.0, step=1000.0)
        c4, c5, c6 = st.columns(3)
        stage = c4.selectbox("Stage", options=db.STAGE_NAMES)
        close_date = c5.date_input("Expected close date", value=date.today())
        notes = c6.text_input("Notes", value="")
        submitted = st.form_submit_button("Add deal")
        if submitted:
            if not name:
                st.warning("Give the deal a name before adding it.")
            else:
                db.add_deal(name, account, amount, stage, close_date.isoformat(), notes)
                st.rerun()

    st.subheader("Pipeline")
    st.caption(
        "Edit cells directly, or select a row's checkbox and press delete to remove it. "
        "Probability and forecast category default from the stage but can be overridden per deal."
    )

    deals_df = pd.DataFrame(deals)
    if deals_df.empty:
        st.info("No deals yet — add one above.")
    else:
        deals_df["expected_close_date"] = pd.to_datetime(deals_df["expected_close_date"]).dt.date
        edited = st.data_editor(
            deals_df,
            key="pipeline_editor",
            num_rows="dynamic",
            use_container_width=True,
            column_config={
                "id": st.column_config.NumberColumn("ID", disabled=True),
                "name": st.column_config.TextColumn("Deal"),
                "account": st.column_config.TextColumn("Account"),
                "amount": st.column_config.NumberColumn("Amount ($)", min_value=0.0, format="$%.0f"),
                "stage": st.column_config.SelectboxColumn("Stage", options=db.STAGE_NAMES),
                "probability": st.column_config.NumberColumn("Probability %", min_value=0, max_value=100),
                "forecast_category": st.column_config.SelectboxColumn(
                    "Forecast Category", options=db.FORECAST_CATEGORIES
                ),
                "expected_close_date": st.column_config.DateColumn("Close Date"),
                "created_date": st.column_config.TextColumn("Created", disabled=True),
                "notes": st.column_config.TextColumn("Notes"),
            },
        )
        if st.button("Save pipeline changes"):
            records = []
            for record in edited.to_dict("records"):
                if not record.get("name"):
                    continue
                stage_defaults = db.STAGE_DEFAULTS.get(record["stage"], {"probability": 50, "forecast_category": "Pipeline"})
                if pd.isna(record.get("probability")):
                    record["probability"] = stage_defaults["probability"]
                if not record.get("forecast_category") or pd.isna(record.get("forecast_category")):
                    record["forecast_category"] = stage_defaults["forecast_category"]
                if pd.isna(record.get("amount")):
                    record["amount"] = 0.0
                record["expected_close_date"] = str(record["expected_close_date"])
                record["account"] = record.get("account") or ""
                record["notes"] = record.get("notes") or ""
                records.append(record)
            db.replace_all_deals(records)
            st.success("Saved.")
            st.rerun()

        st.divider()
        st.subheader("Open pipeline by stage")
        open_deals = [d for d in deals if d["forecast_category"] not in ("Closed Lost", "Omitted")]
        stage_totals = {s: 0.0 for s in db.STAGE_NAMES if s != "Closed Lost"}
        for d in open_deals:
            stage_totals[d["stage"]] = stage_totals.get(d["stage"], 0.0) + d["amount"]
        funnel_stages = [s for s in stage_totals if stage_totals[s] > 0]
        if funnel_stages:
            fig = go.Figure(go.Funnel(
                y=funnel_stages,
                x=[stage_totals[s] for s in funnel_stages],
                marker={"color": colors.FUNNEL_RAMP[: len(funnel_stages)]},
                texttemplate="%{label}<br>$%{value:,.0f}",
                textposition="inside",
            ))
            fig.update_layout(
                paper_bgcolor=colors.CHROME["surface"],
                plot_bgcolor=colors.CHROME["surface"],
                font_color=colors.CHROME["primary_ink"],
                margin=dict(t=10, b=10),
            )
            st.plotly_chart(fig, use_container_width=True)
        else:
            st.info("No open pipeline to chart yet.")

# ---------------------------------------------------------------------------
# Forecast tab
# ---------------------------------------------------------------------------
with tab_forecast:
    st.subheader(f"Bookings forecast — {forecast.SCENARIO_LABELS[scenario]}")

    if forecast_rows:
        periods = [r["period_label"] for r in forecast_rows]
        bookings = [r["bookings"] for r in forecast_rows]
        quota = [r["quota"] for r in forecast_rows]

        fig = go.Figure()
        fig.add_bar(
            x=periods, y=bookings, name="Forecasted bookings",
            marker_color=colors.CATEGORICAL[0],
            marker_line=dict(color=colors.CHROME["surface"], width=2),
            text=[money(v) for v in bookings], textposition="outside",
        )
        fig.add_trace(go.Scatter(
            x=periods, y=quota, name="Quota", mode="lines+markers",
            line=dict(color=colors.CATEGORICAL[7], dash="dash", width=2),
            marker=dict(size=8),
        ))
        fig.update_layout(
            paper_bgcolor=colors.CHROME["surface"],
            plot_bgcolor=colors.CHROME["surface"],
            font_color=colors.CHROME["primary_ink"],
            yaxis=dict(title="Dollars", gridcolor=colors.CHROME["gridline"]),
            xaxis=dict(title=None),
            legend=dict(orientation="h", yanchor="bottom", y=1.02),
            margin=dict(t=40, b=10),
        )
        st.plotly_chart(fig, use_container_width=True)

        table_df = pd.DataFrame(forecast_rows)[
            ["period_label", "quota", "bookings", "attainment_pct", "deal_count"]
        ].rename(columns={
            "period_label": "Period", "quota": "Quota", "bookings": "Bookings",
            "attainment_pct": "Attainment %", "deal_count": "# Deals",
        })

        def style_attainment(val):
            status = forecast.attainment_status(val)
            return f"color: {colors.STATUS[status]}; font-weight: 600"

        st.dataframe(
            table_df.style
                .format({"Quota": money, "Bookings": money, "Attainment %": "{:.0f}%"})
                .map(style_attainment, subset=["Attainment %"]),
            use_container_width=True,
        )
    else:
        st.info("No forecast periods to show.")

# ---------------------------------------------------------------------------
# Earnings tab
# ---------------------------------------------------------------------------
with tab_earnings:
    st.subheader("Projected earnings")

    closed_won_ytd = sum(
        d["amount"] for d in deals
        if d["forecast_category"] == "Closed Won"
        and forecast.parse_date(d["expected_close_date"]).year == date.today().year
    )
    total_projected = sum(r["total_earnings"] for r in forecast_rows)

    c1, c2, c3 = st.columns(3)
    c1.metric("Closed-won bookings (YTD)", money(closed_won_ytd))
    c2.metric("Annual quota", money(comp_plan["annual_quota"]))
    c3.metric(f"Projected earnings — next {num_periods} {period_type_label}", money(total_projected))

    if forecast_rows:
        periods = [r["period_label"] for r in forecast_rows]
        base = [r["base"] for r in forecast_rows]
        commission = [r["commission"] for r in forecast_rows]

        fig = go.Figure()
        fig.add_bar(
            x=periods, y=base, name="Base salary",
            marker_color=colors.EARNINGS_COLORS["base"],
            marker_line=dict(color=colors.CHROME["surface"], width=2),
        )
        fig.add_bar(
            x=periods, y=commission, name="Commission",
            marker_color=colors.EARNINGS_COLORS["commission"],
            marker_line=dict(color=colors.CHROME["surface"], width=2),
        )
        fig.update_layout(
            barmode="stack",
            paper_bgcolor=colors.CHROME["surface"],
            plot_bgcolor=colors.CHROME["surface"],
            font_color=colors.CHROME["primary_ink"],
            yaxis=dict(title="Dollars", gridcolor=colors.CHROME["gridline"]),
            legend=dict(orientation="h", yanchor="bottom", y=1.02),
            margin=dict(t=40, b=10),
        )
        st.plotly_chart(fig, use_container_width=True)

        earnings_table = pd.DataFrame(forecast_rows)[
            ["period_label", "base", "commission", "total_earnings"]
        ].rename(columns={
            "period_label": "Period", "base": "Base", "commission": "Commission",
            "total_earnings": "Total",
        })
        st.dataframe(
            earnings_table.style.format({"Base": money, "Commission": money, "Total": money}),
            use_container_width=True,
        )

# ---------------------------------------------------------------------------
# Comp Plan tab
# ---------------------------------------------------------------------------
with tab_comp_plan:
    st.subheader("Compensation plan")
    st.caption("Quota and base salary are entered as annual figures and split evenly across the quota period.")

    with st.form("comp_plan_form"):
        cp1, cp2 = st.columns(2)
        annual_quota = cp1.number_input("Annual quota ($)", min_value=0.0, value=float(comp_plan["annual_quota"]), step=10000.0)
        annual_base = cp2.number_input("Annual base salary ($)", min_value=0.0, value=float(comp_plan["annual_base_salary"]), step=1000.0)
        quota_period = st.selectbox(
            "Quota period", options=["monthly", "quarterly"],
            index=["monthly", "quarterly"].index(comp_plan["quota_period"]),
        )

        st.markdown("**Commission tiers** — rate applied to the slice of bookings within each attainment band.")
        tiers_df = pd.DataFrame(comp_plan["commission_tiers"])
        tiers_edited = st.data_editor(
            tiers_df,
            key="tiers_editor",
            num_rows="dynamic",
            use_container_width=True,
            column_config={
                "up_to_pct": st.column_config.NumberColumn(
                    "Up to attainment %  (blank = uncapped)", min_value=0
                ),
                "rate_pct": st.column_config.NumberColumn("Commission rate %", min_value=0, max_value=100),
            },
        )

        if st.form_submit_button("Save comp plan"):
            tiers = []
            for record in tiers_edited.to_dict("records"):
                if pd.isna(record.get("rate_pct")):
                    continue
                up_to = record.get("up_to_pct")
                tiers.append({
                    "up_to_pct": None if pd.isna(up_to) else float(up_to),
                    "rate_pct": float(record["rate_pct"]),
                })
            tiers.sort(key=lambda t: (t["up_to_pct"] is None, t["up_to_pct"]))
            new_plan = {
                "annual_quota": annual_quota,
                "annual_base_salary": annual_base,
                "quota_period": quota_period,
                "commission_tiers": tiers,
            }
            db.save_comp_plan(new_plan)
            st.success("Comp plan saved.")
            st.rerun()

# ---------------------------------------------------------------------------
# Export tab
# ---------------------------------------------------------------------------
with tab_export:
    st.subheader("Export")
    st.caption(
        "Excel and plain CSV export the full pipeline/forecast tables. "
        "The Simplifi exports use the Date / Payee / Amount / Tags columns "
        "Quicken Simplifi's manual CSV importer expects (dates as M/D/YYYY)."
    )

    st.download_button(
        "Download Excel workbook (Pipeline + Forecast + Comp Plan)",
        data=export.export_excel_workbook(deals, forecast_rows, comp_plan),
        file_name="sales_pipeline_forecast.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )

    c1, c2 = st.columns(2)
    with c1:
        st.download_button(
            "Download pipeline CSV",
            data=export.export_pipeline_csv(deals),
            file_name="pipeline.csv",
            mime="text/csv",
        )
        st.download_button(
            "Download forecast CSV",
            data=export.export_forecast_csv(forecast_rows),
            file_name="forecast.csv",
            mime="text/csv",
        )
    with c2:
        scenario_amounts = {d["id"]: forecast.scenario_deal_amount(d, scenario) for d in deals}
        st.download_button(
            "Download Simplifi CSV — per-deal projected income",
            data=export.export_simplifi_deals_csv(deals, scenario_amounts),
            file_name="simplifi_deals.csv",
            mime="text/csv",
        )
        st.download_button(
            "Download Simplifi CSV — per-period projected earnings",
            data=export.export_simplifi_earnings_csv(forecast_rows),
            file_name="simplifi_earnings.csv",
            mime="text/csv",
        )
