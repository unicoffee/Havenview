#!/usr/bin/env python3
"""Havenview — Layer D: dashboard (Streamlit) + alerter.

OBSERVE-AND-NOTIFY ONLY
------------------------
This module never trades and never writes to a brokerage. It has two parts
that share the same data-loading and alert-evaluation logic but run on
different triggers:

- **The Streamlit app** (``streamlit run layer_d_dashboard.py``) is a
  read-only viewer. It never writes to the DB and never POSTs to a webhook
  merely because the page was opened or rerun — Streamlit reruns the whole
  script on every widget interaction, so wiring alert delivery into the
  render path would spam duplicate DB rows and duplicate webhook POSTs on
  every click. The dashboard shows a *preview* of what the alerter would
  fire (via the pure ``evaluate_alerts()``) and reads already-recorded
  alerts from the ``alerts`` table; actually routing alerts only happens
  behind an explicit "Run alert check now" button.
- **The alerter** (``run_alerter()``) is the side-effecting piece: it
  evaluates conditions and writes to ``alerts`` (always) and POSTs RED
  alerts to ``PUSH_WEBHOOK_URL`` (if configured). It's meant to be called as
  a discrete pipeline step — by ``run_daily.py`` after Layer C, or manually
  via ``python layer_d_dashboard.py --alert-check``.

Both read from data Layers A/B/C already collected — this module makes no
live market/news/prediction-market calls of its own. It reads the latest
Layer A snapshot, calls Layer B's pure ``compute_portfolio()``, and reads the
most recently persisted ``macro_history``/``predmkt_history`` rows (written
by a prior ``layer_c_macro.py`` run). The one exception is the catalyst
calendar, which is pure date arithmetic on ``watches.json`` and is safe to
recompute on every render.

Alert routing rules
--------------------
- **AMBER** -> appended to the ``alerts`` table (and printed to the log)
  only. Covers: sleeve-cap breaches, per-name cap breaches, DTE flags
  (theta-bleed / IV-crush / expired-leg), an IV-rank spike past a watch's
  ``iv_rank_spike`` threshold, WBD's spread converging (deal closing —
  informational), and a WBD "merger completed" headline match.
- **RED** -> POSTed to ``PUSH_WEBHOOK_URL`` if set (else logged same as
  AMBER, plus still written to ``alerts``). Fires on:
    - any ``stop_underlying`` breach (any position)
    - WBD: arb-spread widen > threshold (break-risk repricing) OR a
      break-type headline match (state AG suit, EU/FCC review, deal
      blocked/terminated — "merger completed" is excluded, that's the
      *good* outcome, not a break)
    - TTWO: any delay-keyword headline match OR a Kalshi on-time-probability
      drop >= the configured points threshold
    - RKLB: any slip-keyword headline match OR the underlying trading at/below
      its configured support/break level (a "decisive break" is treated
      here as spot <= support — the same breach convention used everywhere
      else in this codebase; refining that to a multi-day confirmation is a
      reasonable future enhancement, not implemented in this skeleton)

Headline-based conditions only evaluate when a headlines file is supplied
(``--headlines``, matching Layer C's part 4 design: there is no live news
feed wired into this project, so those specific conditions simply don't
fire on a run where no headlines were sourced this cycle — that is expected,
not a bug.

Usage:
    streamlit run layer_d_dashboard.py           # the dashboard
    python layer_d_dashboard.py --alert-check     # run the alerter once, headless
    python layer_d_dashboard.py --alert-check --headlines headlines.json
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone

import layer_a_data as la_data
import layer_b_portfolio as lb
import layer_c_macro as lc
from layer_a_data import find_latest_asof

try:
    from dotenv import load_dotenv
except ImportError:
    def load_dotenv(*_args, **_kwargs):
        return False

POSITIONS_FILE = "positions.json"
WATCHES_FILE = "watches.json"

# Macro-dial color bands (documented, tunable — not a single-name veto).
MACRO_BAND_RED = 30.0
MACRO_BAND_GREEN = 70.0

# WBD headline keywords that indicate the deal is at risk vs. the keyword
# that indicates it closed successfully (excluded from the "break" trigger).
WBD_BREAK_KEYWORDS = {
    "state attorney general sue", "EU Foreign Subsidies",
    "FCC foreign ownership", "deal blocked", "merger terminated",
}
WBD_COMPLETED_KEYWORD = "merger completed"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ==========================================================================
# Data loading — all read-only.
# ==========================================================================
def load_latest_macro_score(conn: sqlite3.Connection) -> dict | None:
    """The most recent macro_history batch (all series share one write's
    timestamp — see layer_c_macro.persist_macro_history)."""
    row = conn.execute("SELECT MAX(ts) FROM macro_history").fetchone()
    if not row or not row[0]:
        return None
    latest_ts = row[0]
    series = dict(conn.execute(
        "SELECT series, value FROM macro_history WHERE ts = ?", (latest_ts,)
    ).fetchall())
    if not series:
        return None
    return {"ts": latest_ts, "series": series}


def load_latest_predmkt(conn: sqlite3.Connection, market: str) -> dict | None:
    row = conn.execute(
        "SELECT ts, ticker, yes_price, no_price, volume, raw FROM predmkt_history "
        "WHERE market = ? ORDER BY ts DESC LIMIT 1",
        (market,),
    ).fetchone()
    if not row:
        return None
    ts, ticker, yes_price, no_price, volume, raw = row
    parsed_raw = json.loads(raw) if raw else {}
    return {
        "ts": ts, "market": market, "ticker": ticker,
        "yes_price": yes_price, "no_price": no_price, "volume": volume,
        "raw": parsed_raw,
    }


def load_bundle(
    db_path: str = la_data.DEFAULT_DB,
    data_dir: str = la_data.DEFAULT_DATA_DIR,
    positions_path: str = POSITIONS_FILE,
    watches_path: str = WATCHES_FILE,
    asof: str | None = None,
    headlines_path: str | None = None,
) -> dict:
    """Load everything the dashboard/alerter needs, read-only. Raises a
    clear SystemExit if no Layer A snapshot has ever been collected."""
    resolved_asof = asof or find_latest_asof(data_dir, db_path)
    if not resolved_asof:
        raise SystemExit(
            f"No Layer A snapshot found in {data_dir} or {db_path}. "
            "Run layer_a_data.py first."
        )
    snapshot = la_data.replay(resolved_asof, data_dir, db_path)

    with open(positions_path) as fh:
        positions = json.load(fh)
    with open(watches_path) as fh:
        watches = json.load(fh)

    portfolio = lb.compute_portfolio(snapshot, positions)
    catalyst_calendar = lc.compute_catalyst_calendar(watches, resolved_asof)

    conn = sqlite3.connect(db_path)
    try:
        macro = load_latest_macro_score(conn)
        predmkt = {
            "TTWO": load_latest_predmkt(conn, f"kalshi:{lc.KALSHI_TTWO_SERIES}"),
            "WBD": load_latest_predmkt(conn, "wbd:psky_arb_spread"),
        }
    finally:
        conn.close()

    news_matches = {}
    if headlines_path:
        with open(headlines_path) as fh:
            headlines = json.load(fh)
        news_matches = lc.scan_headlines(headlines)

    return {
        "asof": resolved_asof,
        "generated_at": utc_now_iso(),
        "snapshot": snapshot,
        "positions": positions,
        "watches": watches,
        "portfolio": portfolio,
        "catalyst_calendar": catalyst_calendar,
        "macro": macro,
        "predmkt": predmkt,
        "news_matches": news_matches,
    }


# ==========================================================================
# Alert evaluation — pure, no side effects. Safe to call on every Streamlit
# render for the live preview.
# ==========================================================================
def _watch_by_ticker(watches: list, ticker: str) -> dict:
    return next((w for w in watches if w["ticker"] == ticker), {})


def evaluate_alerts(bundle: dict) -> list:
    alerts = []
    ts = utc_now_iso()
    portfolio = bundle["portfolio"]
    watches = bundle["watches"]
    snapshot = bundle["snapshot"]
    predmkt = bundle["predmkt"]
    news = bundle.get("news_matches") or {}

    def add(severity, ticker, tripwire, message, context=None):
        alerts.append({
            "ts": ts, "ticker": ticker, "severity": severity,
            "tripwire": tripwire, "message": message, "context": context or {},
        })

    # --- RED: any stop_underlying breach (applies to every position) ---
    for pos in portfolio["stop_breaches"]:
        add(
            "RED", pos["ticker"], "stop_underlying_breach",
            f"{pos['ticker']} underlying {pos['underlying']} at/below stop "
            f"{pos['stop_underlying']} ({pos['stop_distance_pct']:+.2f}%)",
            {"underlying": pos["underlying"], "stop": pos["stop_underlying"]},
        )

    # --- WBD: spread widen / break headline (RED); converge / merger
    #     completed (AMBER, informational) ---
    wbd_pm = predmkt.get("WBD")
    wbd_hits = news.get("WBD", [])
    wbd_break_hits = [h for h in wbd_hits if set(h["matched_keywords"]) & WBD_BREAK_KEYWORDS]
    wbd_completed_hits = [h for h in wbd_hits if WBD_COMPLETED_KEYWORD in h["matched_keywords"]]

    if wbd_pm and wbd_pm["raw"].get("widened"):
        add("RED", "WBD", "wbd_spread_widen",
            f"WBD arb spread widened to {wbd_pm['raw']['spread_pct']}% "
            f"(> {wbd_pm['raw']['widen_threshold']}%) — break-risk repricing",
            wbd_pm["raw"])
    if wbd_break_hits:
        add("RED", "WBD", "wbd_break_headline",
            "WBD break-risk headline match: " + "; ".join(h["text"] for h in wbd_break_hits),
            {"matches": wbd_break_hits})
    if wbd_pm and wbd_pm["raw"].get("converged"):
        add("AMBER", "WBD", "wbd_spread_converge",
            f"WBD arb spread converged to {wbd_pm['raw']['spread_pct']}% "
            f"(< {wbd_pm['raw']['converge_threshold']}%) — deal appears to be closing",
            wbd_pm["raw"])
    if wbd_completed_hits:
        add("AMBER", "WBD", "wbd_merger_completed_headline",
            "WBD merger-completed headline match (informational): "
            + "; ".join(h["text"] for h in wbd_completed_hits),
            {"matches": wbd_completed_hits})

    # --- TTWO: delay headline / Kalshi on-time drop (RED); IV-rank spike
    #     (AMBER) ---
    ttwo_pm = predmkt.get("TTWO")
    ttwo_hits = news.get("TTWO", [])
    if ttwo_hits:
        add("RED", "TTWO", "ttwo_delay_headline",
            "TTWO delay headline match: " + "; ".join(h["text"] for h in ttwo_hits),
            {"matches": ttwo_hits})
    if ttwo_pm and ttwo_pm["raw"].get("flag_drop"):
        add("RED", "TTWO", "ttwo_kalshi_ontime_drop",
            f"Kalshi TTWO on-time probability dropped {ttwo_pm['raw']['drop_pts']}pts "
            f"(>= {ttwo_pm['raw']['drop_threshold']}pts)",
            ttwo_pm["raw"])

    # --- RKLB: slip headline / decisive break of support (RED) ---
    rklb_watch = _watch_by_ticker(watches, "RKLB")
    rklb_hits = news.get("RKLB", [])
    if rklb_hits:
        add("RED", "RKLB", "rklb_slip_headline",
            "RKLB slip headline match: " + "; ".join(h["text"] for h in rklb_hits),
            {"matches": rklb_hits})
    rklb_snap = (snapshot.get("tickers", {}) or {}).get("RKLB") or {}
    rklb_spot = rklb_snap.get("underlying")
    support = rklb_watch.get("support_level") or (rklb_watch.get("tripwires") or {}).get("break_support")
    if rklb_spot is not None and support is not None and rklb_spot <= support:
        add("RED", "RKLB", "rklb_break_support",
            f"RKLB underlying {rklb_spot} at/below support {support}",
            {"underlying": rklb_spot, "support": support})

    # --- AMBER: sleeve / name-cap / DTE flags (from Layer B) ---
    for sb in portfolio["sleeve_breaches"]:
        add("AMBER", None, "sleeve_cap_breach",
            f"Sleeve '{sb['sleeve']}' at {sb['total_size_pct_reserve']}% "
            f"(> cap {sb['cap_pct']}%): {', '.join(sb['tickers'])}",
            sb)
    for nb in portfolio["name_cap_breaches"]:
        add("AMBER", nb["ticker"], "name_cap_breach",
            f"{nb['ticker']} at {nb['size_pct_reserve']}% of reserve "
            f"(> per-name cap {nb['name_cap_pct']}%)",
            {"size_pct_reserve": nb["size_pct_reserve"], "cap": nb["name_cap_pct"]})
    for flag in portfolio["dte_flags"]:
        add("AMBER", flag["ticker"], flag["flag"], flag["detail"], flag)

    # --- AMBER: IV-rank spike (TTWO / RKLB tripwire) ---
    for ticker in ("TTWO", "RKLB"):
        watch = _watch_by_ticker(watches, ticker)
        threshold = (watch.get("tripwires") or {}).get("iv_rank_spike")
        ivr = ((snapshot.get("tickers", {}) or {}).get(ticker) or {}).get("iv_rank") or {}
        rank = ivr.get("iv_rank")
        if threshold is not None and rank is not None and rank >= threshold:
            add("AMBER", ticker, "iv_rank_spike",
                f"{ticker} IV-rank {rank} >= spike threshold {threshold}",
                {"iv_rank": rank, "threshold": threshold})

    return alerts


# ==========================================================================
# Alert routing — side-effecting. Call once per pipeline run, never per
# Streamlit rerun.
# ==========================================================================
def post_webhook(url: str, alert: dict) -> bool:
    import requests

    try:
        resp = requests.post(url, json=alert, timeout=10)
        return 200 <= resp.status_code < 300
    except Exception as exc:  # noqa: BLE001 — never let a bad webhook crash the alerter
        print(f"[alerter] webhook POST failed: {exc}", file=sys.stderr)
        return False


def route_alerts(alerts: list, conn: sqlite3.Connection, webhook_url: str | None) -> dict:
    """Append-only: every call inserts fresh rows (alerts are events, not a
    per-day snapshot — the same condition firing again tomorrow is a new,
    legitimate alert). RED gets an additional webhook POST attempt."""
    n_amber = n_red = n_delivered = n_failed = 0
    for a in alerts:
        delivered = 0
        if a["severity"] == "RED":
            n_red += 1
            if webhook_url:
                ok = post_webhook(webhook_url, a)
                delivered = 1 if ok else 0
                if ok:
                    n_delivered += 1
                else:
                    n_failed += 1
                    print(f"[ALERT][RED][webhook-failed] {a['ticker']} {a['tripwire']}: {a['message']}", file=sys.stderr)
            else:
                print(f"[ALERT][RED][no-webhook-configured] {a['ticker']} {a['tripwire']}: {a['message']}", file=sys.stderr)
        else:
            n_amber += 1
            print(f"[ALERT][AMBER] {a['ticker']} {a['tripwire']}: {a['message']}", file=sys.stderr)

        conn.execute(
            "INSERT INTO alerts (ts, ticker, severity, tripwire, message, context, delivered) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (a["ts"], a["ticker"], a["severity"], a["tripwire"], a["message"],
             json.dumps(a["context"]), delivered),
        )
    conn.commit()
    return {"n_amber": n_amber, "n_red": n_red, "n_webhook_delivered": n_delivered, "n_webhook_failed": n_failed, "n_total": len(alerts)}


def run_alerter(
    db_path: str = la_data.DEFAULT_DB,
    data_dir: str = la_data.DEFAULT_DATA_DIR,
    positions_path: str = POSITIONS_FILE,
    watches_path: str = WATCHES_FILE,
    asof: str | None = None,
    headlines_path: str | None = None,
    webhook_url: str | None = None,
) -> dict:
    """THE alerter step: load -> evaluate -> route. This is what
    run_daily.py (and the dashboard's manual "run alert check" button) call.
    """
    load_dotenv()
    webhook_url = webhook_url or os.getenv("PUSH_WEBHOOK_URL") or None

    bundle = load_bundle(db_path, data_dir, positions_path, watches_path, asof, headlines_path)
    alerts = evaluate_alerts(bundle)

    conn = sqlite3.connect(db_path)
    try:
        summary = route_alerts(alerts, conn, webhook_url)
    finally:
        conn.close()

    summary["asof"] = bundle["asof"]
    summary["alerts"] = alerts
    return summary


# ==========================================================================
# Catalyst Watch panel helpers
# ==========================================================================
def _severity_for_ticker(alerts: list, ticker: str) -> str:
    sevs = [a["severity"] for a in alerts if a["ticker"] == ticker]
    if "RED" in sevs:
        return "RED"
    if "AMBER" in sevs:
        return "AMBER"
    return "GREEN"


def catalyst_watch_rows(bundle: dict, alerts: list) -> list:
    rows = []
    snapshot_tickers = bundle["snapshot"].get("tickers", {}) or {}
    for cal in bundle["catalyst_calendar"]:
        ticker = cal["ticker"]
        snap = snapshot_tickers.get(ticker) or {}
        ivr = (snap.get("iv_rank") or {}).get("iv_rank")

        if ticker == "WBD":
            pm = (bundle["predmkt"].get("WBD") or {}).get("raw") or {}
            key_metric = f"spread {pm.get('spread_pct', 'n/a')}%" if pm else "spread n/a"
        elif ticker == "RKLB":
            watch = _watch_by_ticker(bundle["watches"], "RKLB")
            support = watch.get("support_level")
            spot = snap.get("underlying")
            dist = f"{spot - support:+.2f} vs ${support}" if (spot is not None and support is not None) else "n/a"
            key_metric = f"{dist} · IV-rank {ivr if ivr is not None else 'n/a'}"
        elif ticker == "TTWO":
            pm = bundle["predmkt"].get("TTWO")
            ontime = f"{pm['yes_price']*100:.1f}% on-time ({pm['raw'].get('kalshi_title', '')})" if pm and pm.get("yes_price") is not None else "Kalshi n/a"
            key_metric = f"IV-rank {ivr if ivr is not None else 'n/a'} · {ontime}"
        else:
            key_metric = "n/a"

        rows.append({
            "ticker": ticker,
            "catalyst": cal["catalyst"],
            "days_to_catalyst": cal["days_to_catalyst"],
            "date_precision": cal["date_precision"],
            "status": _severity_for_ticker(alerts, ticker),
            "key_metric": key_metric,
        })
    return rows


# ==========================================================================
# Streamlit UI (only reached in dashboard mode — see __main__ dispatch)
# ==========================================================================
def render_dashboard(db_path, data_dir, positions_path, watches_path, headlines_path):
    import pandas as pd
    import streamlit as st

    st.set_page_config(page_title="Havenview", layout="wide")
    st.title("Havenview — read-only options monitor")
    st.caption("Data-in, alerts-out only. This dashboard never trades and never writes to a brokerage.")

    try:
        bundle = load_bundle(db_path, data_dir, positions_path, watches_path, headlines_path=headlines_path)
    except SystemExit as exc:
        st.error(str(exc))
        return

    st.caption(f"Snapshot as-of **{bundle['asof']}** · rendered {bundle['generated_at']}")
    alerts_preview = evaluate_alerts(bundle)

    # --- Positions table ---
    st.header("Positions")
    rows = []
    for p in bundle["portfolio"]["positions"]:
        pnl = None
        if p.get("position_value") is not None and p.get("net_debit") is not None:
            pnl = round(p["position_value"] - p["net_debit"], 2)
        g = p["position_greeks"]
        rows.append({
            "Ticker": p["ticker"], "Structure": p["structure"], "Sleeve": p["sleeve"],
            "Delta": g["delta"], "Theta/day": g["theta"], "Vega": g["vega"],
            "Stop": p["stop_underlying"], "Dist $": p["stop_distance_dollar"], "Dist %": p["stop_distance_pct"],
            "Stop breached": bool(p["stop_breached"]),
            "P&L $": pnl, "Size % reserve": p["size_pct_reserve"],
        })
    df_pos = pd.DataFrame(rows)
    st.dataframe(
        df_pos.style.apply(
            lambda r: ["background-color: #ffcccc" if r["Stop breached"] else "" for _ in r], axis=1
        ),
        width="stretch",
    )

    col1, col2 = st.columns(2)

    # --- Sleeve allocation vs caps ---
    with col1:
        st.header("Sleeve allocation vs caps")
        sleeve_rows = [
            {"Sleeve": s, "Total % reserve": b["total_size_pct_reserve"], "Cap %": b["cap_pct"],
             "Breached": b["breached"], "Tickers": ", ".join(b["tickers"])}
            for s, b in bundle["portfolio"]["sleeves"].items()
        ]
        df_sleeve = pd.DataFrame(sleeve_rows)
        st.dataframe(
            df_sleeve.style.apply(
                lambda r: ["background-color: #ffcccc" if r["Breached"] else "" for _ in r], axis=1
            ),
            width="stretch",
        )

    # --- Macro-gate scorecard ---
    with col2:
        st.header("Macro gate — deployment-pace dial")
        macro = bundle["macro"]
        if not macro:
            st.info("No macro score collected yet — run layer_c_macro.py.")
        else:
            series = macro["series"]
            composite = series.get("MACRO_SCORE")
            if composite is not None:
                if composite < MACRO_BAND_RED:
                    st.error(f"Composite: {composite} / 100 — risk-off, slow deployment pace")
                elif composite < MACRO_BAND_GREEN:
                    st.warning(f"Composite: {composite} / 100 — neutral")
                else:
                    st.success(f"Composite: {composite} / 100 — risk-on")
                st.progress(min(max(composite / 100.0, 0.0), 1.0))
            st.caption("Dials portfolio-level cash-deployment pace — NOT a single-name veto or stop.")
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("VIX", series.get("VIX_LEVEL"), f"{series.get('VIX_PCT_52WK', 'n/a')} pctl")
            c2.metric("Term structure", series.get("TERM_STRUCTURE_SCORE"), f"ratio {series.get('TERM_STRUCTURE_RATIO', 'n/a')}")
            c3.metric("Breadth % >200DMA", series.get("BREADTH_PCT_ABOVE_200DMA"))
            c4.metric("HY OAS", series.get("HY_OAS_LEVEL"), f"{series.get('HY_OAS_PCT_52WK', 'n/a')} pctl")
            st.caption(f"As of {macro['ts']}")

    # --- Catalyst Watch ---
    st.header("Catalyst Watch")
    cw_rows = catalyst_watch_rows(bundle, alerts_preview)
    df_cw = pd.DataFrame(cw_rows)

    def _status_color(status):
        return {"RED": "background-color: #ffcccc", "AMBER": "background-color: #fff3cd", "GREEN": "background-color: #d4edda"}.get(status, "")

    st.dataframe(
        df_cw.style.apply(lambda r: [_status_color(r["status"]) for _ in r], axis=1),
        width="stretch",
    )

    # --- Alert log + manual trigger ---
    st.header("Alerts")
    st.caption(
        f"Live preview this render: {sum(1 for a in alerts_preview if a['severity']=='RED')} RED, "
        f"{sum(1 for a in alerts_preview if a['severity']=='AMBER')} AMBER "
        "(not yet written — click below to record + notify)."
    )
    if st.button("Run alert check now (writes to DB, POSTs RED to webhook if configured)"):
        summary = run_alerter(db_path, data_dir, positions_path, watches_path, headlines_path=headlines_path)
        st.success(f"Recorded {summary['n_total']} alert(s): {summary['n_red']} RED "
                   f"({summary['n_webhook_delivered']} delivered), {summary['n_amber']} AMBER.")

    conn = sqlite3.connect(db_path)
    try:
        recent = conn.execute(
            "SELECT ts, ticker, severity, tripwire, message, delivered FROM alerts ORDER BY ts DESC LIMIT 50"
        ).fetchall()
    finally:
        conn.close()
    if recent:
        df_alerts = pd.DataFrame(recent, columns=["ts", "ticker", "severity", "tripwire", "message", "delivered"])
        st.dataframe(df_alerts, width="stretch")
    else:
        st.caption("No alerts recorded yet.")


# ==========================================================================
# Entry point — dispatches between headless alert-check and the Streamlit UI.
# ==========================================================================
def main():
    parser = argparse.ArgumentParser(description="Havenview Layer D — dashboard + alerter (observe-and-notify only)")
    parser.add_argument("--db", default=la_data.DEFAULT_DB)
    parser.add_argument("--data-dir", default=la_data.DEFAULT_DATA_DIR)
    parser.add_argument("--positions", default=POSITIONS_FILE)
    parser.add_argument("--watches", default=WATCHES_FILE)
    parser.add_argument("--headlines", default=None)
    parser.add_argument("--alert-check", action="store_true",
                        help="Run the alerter once, headless (no Streamlit), and print the summary")
    args, _unknown = parser.parse_known_args()

    if args.alert_check:
        summary = run_alerter(args.db, args.data_dir, args.positions, args.watches, headlines_path=args.headlines)
        print(json.dumps(summary, indent=2, default=str))
        return

    render_dashboard(args.db, args.data_dir, args.positions, args.watches, args.headlines)


if __name__ == "__main__":
    main()
