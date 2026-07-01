#!/usr/bin/env python3
"""Havenview — daily orchestrator: Layer A -> B -> C -> D-alerter.

READ-ONLY GUARANTEE — REAFFIRMED HERE
---------------------------------------
This process reads and reports only. It never places an order, never
modifies a position, and never writes to any brokerage or trading venue —
there is no such code path anywhere in this pipeline. It:
  - READS free public market data (yfinance, FRED, Wikipedia, Kalshi's
    public market-data API) and your own local config (positions.json,
    watches.json).
  - WRITES only to this project's own local SQLite database (monitor.db)
    and dated JSON snapshots under data/.
  - NOTIFIES by appending to the alerts table and, for RED alerts only,
    POSTing to PUSH_WEBHOOK_URL if you've configured one.
That's the entire footprint: data-in, alerts-out. Nothing here can buy,
sell, hedge, or otherwise touch a live position.

What this script does, in order
---------------------------------
1. **Layer A** (``layer_a_data.py``) — pulls underlying prices + option
   chains, computes Greeks locally, derives IV-rank, evaluates stop
   distances. Writes a dated JSON snapshot + upserts ``snapshots``/
   ``iv_history``.
2. **Layer B** (``layer_b_portfolio.py``) — pure compute: allocation vs.
   sleeve/name caps, aggregate book Greeks, DTE flags, stop breaches. No
   side effects of its own.
3. **Layer C** (``layer_c_macro.py``) — the macro deployment-pace score,
   the catalyst calendar, the Kalshi/WBD-spread prediction-market poll, and
   (if ``--headlines`` is given) the news-keyword digest. Writes
   ``macro_history``/``predmkt_history``.
4. **Layer D alerter** (``layer_d_dashboard.run_alerter``) — evaluates every
   RED/AMBER condition against what steps 1-3 just collected, writes every
   alert to the ``alerts`` table, and POSTs RED alerts to
   ``PUSH_WEBHOOK_URL`` if configured (else just logs them).

Then it prints a one-screen end-of-run summary (macro score, sleeve
allocation vs. cap, any RED/AMBER tripwires, and days-to-catalyst per name).

``--asof YYYY-MM-DD`` passthrough
-----------------------------------
Without ``--asof``, Layer A does a live pull for today and every later step
uses that same date. With ``--asof``, Layer A instead *replays* the stored
snapshot for that date (no live equity pull, no DB write for Layer A) and
every later step uses that date as its reference point too — Layer C's
FRED/Kalshi calls are still live, but percentile windows, catalyst DTE math,
and market-bracket selection are all computed as of that historical date.
Useful for re-running the analysis/alert layers against a previously
collected day without re-pulling market data.

Scheduling this script
------------------------
Run it once a day before market open, or a few times a day (open / midday /
close) — it's idempotent per calendar day (each layer upserts its own day's
row rather than accumulating duplicates), so running it more than once on
the same day just refreshes that day's data.

**cron** (Linux/macOS), e.g. 9:35am and 3:55pm ET on weekdays — adjust the
hours for your timezone and add ``--headlines`` if you're pairing it with a
news search:
    35 9,15 * * 1-5  cd /path/to/Havenview && /path/to/venv/bin/python run_daily.py >> run_daily.log 2>&1

**Windows Task Scheduler**: create a Basic Task, trigger "Daily" (or
"Weekdays"), action "Start a program":
    Program/script:  C:\\path\\to\\venv\\Scripts\\python.exe
    Arguments:        run_daily.py
    Start in:         C:\\path\\to\\Havenview

Launching the dashboard
--------------------------
The Streamlit dashboard is a **separate, long-running process** — start it
whenever you want to look at the book; it only reads what this script (or a
prior run of it) already collected, and never triggers a live pull or an
alert write just by being open:
    streamlit run layer_d_dashboard.py

Usage:
    python run_daily.py                        # live pull, full pipeline
    python run_daily.py --asof 2026-06-30       # replay Layer A; live Layer C as-of that date
    python run_daily.py --headlines headlines.json
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from types import SimpleNamespace

import init_db
import layer_a_data as la_data
import layer_b_portfolio as lb
import layer_c_macro as lc
import layer_d_dashboard as ld

POSITIONS_FILE = "positions.json"
WATCHES_FILE = "watches.json"


def run_layer_a(db_path, data_dir, asof, sync_rh):
    if asof:
        print(f"[run_daily] Layer A: replaying stored snapshot for {asof} (no live pull)")
        return la_data.replay(asof, data_dir, db_path)
    print("[run_daily] Layer A: live pull...")
    args = SimpleNamespace(db=db_path, data_dir=data_dir, sync_rh=sync_rh)
    return la_data.run_live(args)


def format_summary(snapshot, portfolio, macro_result, alert_summary) -> str:
    lines = []
    lines.append("=" * 72)
    lines.append(f"HAVENVIEW DAILY SUMMARY — as of {snapshot.get('asof')}")
    lines.append("=" * 72)

    # Macro score
    macro = macro_result.get("macro_score") or {}
    composite = macro.get("composite")
    if composite is None:
        lines.append("Macro score: unavailable")
    else:
        band = "RISK-OFF" if composite < 30 else ("RISK-ON" if composite >= 70 else "NEUTRAL")
        lines.append(f"Macro score: {composite}/100 ({band}) — deployment-pace dial, not a single-name veto")
        comp = macro.get("components", {})
        parts = []
        for name in ("vix", "term_structure", "breadth", "credit"):
            c = comp.get(name, {})
            parts.append(f"{name}={c.get('score') if c.get('available') else 'n/a'}")
        lines.append("  components: " + ", ".join(parts))

    # Sleeve allocation vs cap
    lines.append("")
    lines.append("Sleeve allocation vs. cap:")
    for sleeve, b in portfolio["sleeves"].items():
        flag = " *** BREACH ***" if b["breached"] else ""
        lines.append(
            f"  {sleeve}: {b['total_size_pct_reserve']}% (cap {b['cap_pct']}%) "
            f"[{', '.join(b['tickers'])}]{flag}"
        )

    # Tripwires
    lines.append("")
    alerts = alert_summary.get("alerts", [])
    red = [a for a in alerts if a["severity"] == "RED"]
    amber = [a for a in alerts if a["severity"] == "AMBER"]
    lines.append(f"Tripwires this run: {len(red)} RED, {len(amber)} AMBER")
    for a in red:
        lines.append(f"  [RED]   {a['ticker'] or '-'}: {a['tripwire']} — {a['message']}")
    for a in amber:
        lines.append(f"  [AMBER] {a['ticker'] or '-'}: {a['tripwire']} — {a['message']}")
    if not alerts:
        lines.append("  (none)")
    if red:
        delivered = alert_summary.get("n_webhook_delivered", 0)
        failed = alert_summary.get("n_webhook_failed", 0)
        lines.append(f"  webhook: {delivered} delivered, {failed} failed/not-configured")

    # Catalyst calendar
    lines.append("")
    lines.append("Days to catalyst:")
    for cal in macro_result.get("catalyst_calendar", []):
        precision_note = "" if cal["date_precision"] == "exact" else f" ({cal['date_precision']})"
        dte = cal["days_to_catalyst"]
        dte_txt = f"{dte}d" if dte is not None else "n/a"
        lines.append(f"  {cal['ticker']}: {cal['catalyst']} — {dte_txt}{precision_note}")

    lines.append("=" * 72)
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Havenview daily orchestrator: A -> B -> C -> D-alerter (read-only, never trades)")
    parser.add_argument("--db", default=la_data.DEFAULT_DB)
    parser.add_argument("--data-dir", default=la_data.DEFAULT_DATA_DIR)
    parser.add_argument("--positions", default=POSITIONS_FILE)
    parser.add_argument("--watches", default=WATCHES_FILE)
    parser.add_argument("--asof", metavar="YYYY-MM-DD",
                        help="Replay Layer A's stored snapshot for this date instead of a live pull; "
                             "Layers B-D use this as their reference date too")
    parser.add_argument("--headlines", metavar="PATH", help="JSON file of headlines for Layer C's news scan")
    parser.add_argument("--skip-breadth", action="store_true", help="Skip Layer C's heavy S&P 500 breadth pull")
    parser.add_argument("--sync-rh", action="store_true", help="Layer A: also READ Robinhood positions (read-only)")
    args = parser.parse_args()

    # Safety net: init_db.init_db() is idempotent (CREATE TABLE IF NOT EXISTS),
    # so this is harmless if you already ran it — but it means a fresh clone
    # can just run this script without a separate setup step being mandatory.
    init_db.init_db(args.db)

    # --- Layer A ---
    snapshot = run_layer_a(args.db, args.data_dir, args.asof, args.sync_rh)
    asof_date = snapshot["asof"]

    # --- Layer B ---
    print("[run_daily] Layer B: portfolio analytics...")
    with open(args.positions) as fh:
        positions = json.load(fh)
    portfolio = lb.compute_portfolio(snapshot, positions)

    # --- Layer C ---
    print("[run_daily] Layer C: macro gate, catalysts, prediction markets...")
    macro_result = lc.run_layer_c(
        db_path=args.db, data_dir=args.data_dir, watches_path=args.watches,
        asof=asof_date, skip_breadth=args.skip_breadth,
        headlines_path=args.headlines, snapshot=snapshot,
    )

    # --- Layer D: alerter ---
    print("[run_daily] Layer D: evaluating tripwires and routing alerts...")
    alert_summary = ld.run_alerter(
        db_path=args.db, data_dir=args.data_dir,
        positions_path=args.positions, watches_path=args.watches,
        asof=asof_date, headlines_path=args.headlines,
    )

    print()
    print(format_summary(snapshot, portfolio, macro_result, alert_summary))
    print()
    print("Dashboard (separate, read-only, run any time): streamlit run layer_d_dashboard.py")


if __name__ == "__main__":
    main()
