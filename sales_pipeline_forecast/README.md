# Sales Pipeline & Earnings Forecast

A single Streamlit app for a quota-carrying rep: track your pipeline,
forecast bookings, project your commission earnings, and export it all to
Excel, plain CSV, or a Quicken Simplifi-compatible CSV.

Everything is local — one SQLite file (`pipeline.db`), no external accounts,
no network calls. It's a standalone project living inside the Havenview
repo; it doesn't touch the options-monitor code elsewhere in this repo.

## Setup

```bash
cd sales_pipeline_forecast
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
streamlit run app.py
```

First run creates `pipeline.db` and seeds it with 8 example deals so the
forecast/earnings views aren't empty — delete them from the **Pipeline**
tab whenever you're ready to enter your own.

## How it works

### Deals & stages

Each deal has a `stage` (Prospecting → Qualification → Needs Analysis →
Proposal → Negotiation → Closed Won / Closed Lost) and an independent
`forecast_category` (Pipeline / Best Case / Commit / Closed Won / Closed
Lost / Omitted) — the same two-axis model Salesforce forecasting uses.
Picking a stage sets sensible probability/category defaults, but you can
override either per deal (e.g. a Negotiation-stage deal you're not
confident in can stay in the "Pipeline" forecast category).

### Scenarios

The **Forecast** and **Earnings** tabs price your pipeline three ways
(pick one in the sidebar):

- **Weighted** — every open deal counts at `amount x probability%`.
- **Best case** — deals flagged Best Case or Commit count at full value.
- **Commit** — only deals flagged Commit count, at full value.

Closed Won deals always count at full value in every scenario (they're
realized, not projected); Closed Lost / Omitted never count.

### Comp plan & commission

On the **Comp Plan** tab, set your annual quota, annual base salary, and
whether quota is tracked monthly or quarterly. Quota and base salary are
split evenly across periods. Commission is computed with configurable
**tiers** — each tier's rate applies only to the slice of bookings that
falls within that tier's quota-attainment band, so accelerators work the
way most comp plans actually do. Example default:

| Attainment band | Rate |
| --- | --- |
| 0–100% of quota | 8% |
| 100–150% of quota | 12% |
| 150%+ of quota | 16% |

$150K of bookings against a $100K quota under that plan pays
`$100K x 8% + $50K x 12% = $14,000`, not `$150K x 8%` or `$150K x 16%`.

### Export

- **Excel workbook** — Pipeline, Forecast, and Comp Plan as separate sheets.
- **Plain CSV** — pipeline or forecast table, for Excel/Sheets/anything else.
- **Quicken Simplifi CSV** — two variants, both using the `Date, Payee,
  Amount, Tags` columns and `M/D/YYYY` dates Simplifi's manual CSV importer
  expects:
  - *per-deal projected income* — one row per open/won deal, priced at the
    currently selected scenario's amount.
  - *per-period projected earnings* — one row per forecast period (base +
    commission combined), dated to the period's start.

## Files

| Path | Purpose |
| --- | --- |
| `app.py` | Streamlit UI — Pipeline, Forecast, Earnings, Comp Plan, Export tabs |
| `db.py` | SQLite schema + CRUD for deals and comp-plan settings |
| `forecast.py` | Pure functions: period bucketing, scenario pricing, tiered commission math |
| `export.py` | Excel / CSV / Simplifi-CSV builders |
| `colors.py` | Chart color tokens (validated categorical/status/sequential palette) |
| `pipeline.db` | Local SQLite store (gitignored, created on first run) |
