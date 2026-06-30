# Havenview

A **read-only** options-portfolio monitor. Havenview watches an options book
and a set of catalysts, records what it sees, and raises alerts when a
tripwire fires. This repository is the project **skeleton** — data models,
storage, and configuration are in place; the data-collection and alerting
logic are stubs to be filled in.

## Read-only guarantee

> **Havenview never places trades and never writes to any brokerage.**
> It is **data-in, alerts-out** only.

- It **reads** market and prediction-market data (via `yfinance`, an optional
  read-only `KALSHI_API_KEY`, and similar public sources).
- It **writes** only to its own local SQLite database (`monitor.db`) and to
  dated JSON snapshots under `data/`.
- It **emits** alerts to logs and, optionally, to a single outbound webhook
  (`PUSH_WEBHOOK_URL`).
- There is **no brokerage connection, no order entry, and no authentication
  to any trading venue** anywhere in this codebase. No code path can submit,
  modify, or cancel an order.

If you extend Havenview, preserve this guarantee: keep all integrations
restricted to read endpoints and outbound notifications.

## Layout

| Path | Purpose |
| --- | --- |
| `requirements.txt` | Python dependencies |
| `.env.example` | Template for optional secrets (copy to `.env`) |
| `positions.json` | The book: positions, legs, stops (on the **underlying**), targets |
| `watches.json` | Catalyst / event-driven watches and their tripwires |
| `init_db.py` | Creates the SQLite schema in `monitor.db` |
| `layer_a_data.py` | Layer A collector: prices, local BS Greeks, IV-rank, stops |
| `monitor.db` | Local store: `snapshots`, `iv_history`, `macro_history`, `predmkt_history`, `alerts` |
| `data/` | Dated JSON snapshots (e.g. `data/2026-06-30.json`) |
| `theses/` | Per-position thesis documents referenced by `thesis_ref` |

### Configuration notes

- **`positions.json`** — each position has `ticker`, `structure`
  (`LEAP_call` | `call_spread` | `diagonal` | `shares` | …), `legs`
  (`right`, `strike`, `expiry`, `qty`), `net_debit`, `size_pct_reserve`,
  `stop_underlying` (a **price on the underlying**, not a premium),
  `targets`, `catalyst_date`, `thesis_ref`, and `sleeve`
  (`expensive_momentum` | `merger_arb` | …).
- **`watches.json`** — catalyst (`TTWO`, `RKLB`) and event-driven (`WBD`)
  watches, each with a `tripwires` block of thresholds.

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # fill in optional keys if you have them
```

Both environment variables are **optional**:

- `KALSHI_API_KEY` — read-only prediction-market quotes.
- `PUSH_WEBHOOK_URL` — outbound alert delivery (Slack/Discord/ntfy/…).

## Run order

1. **Initialize the database** (idempotent):
   ```bash
   python init_db.py
   ```
2. **Collect a snapshot (Layer A)** — pull underlying prices + history via
   yfinance, fetch each option leg from the chain, compute Greeks **locally**
   with Black-Scholes, derive an IV-rank per name, and evaluate each
   position's distance from its `stop_underlying`. Writes a dated JSON file to
   `data/` and upserts into `snapshots` and `iv_history`:
   ```bash
   python layer_a_data.py                 # live pull for every ticker
   python layer_a_data.py --asof 2026-06-30   # replay a stored snapshot (no network)
   python layer_a_data.py --sync-rh       # also READ Robinhood positions (optional, off by default)
   ```
   IV-rank is flagged as "thin" and seeded from trailing realized vol until at
   least 20 observations have accumulated. `--asof` is purely read-only: it
   reloads a stored snapshot and never touches the network or mutates the DB.
   *(prediction-market + macro collectors — to be implemented)*
3. **Evaluate tripwires** — compare the latest snapshot against the
   thresholds in `watches.json` and write any firings to `alerts`.
   *(evaluator — to be implemented)*
4. **Deliver alerts** — push undelivered `alerts` rows to `PUSH_WEBHOOK_URL`
   if configured; otherwise they remain in the database and logs.
   *(notifier — to be implemented)*
5. **Inspect** — run the dashboard:
   ```bash
   streamlit run app.py        # dashboard — to be implemented
   ```

Steps 2–4 are intended to run on a schedule (e.g. cron / market hours).
Step 1 only needs to run once per environment.
