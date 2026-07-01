#!/usr/bin/env python3
"""Havenview — Layer C: macro gate, catalyst calendar, prediction markets, news scan.

READ-ONLY GUARANTEE
--------------------
Data-in, alerts-out only. This module reads free public data sources (FRED,
Wikipedia's S&P 500 constituent list, yfinance, Kalshi's public market-data
API) and writes observations to ``macro_history`` / ``predmkt_history``. It
NEVER connects to a brokerage or a prediction-market venue to trade — Kalshi
is read via public, unauthenticated GET endpoints only; no order endpoint is
ever imported or called.

Four parts:

1. **Macro score (0-100)** — "the deployment-pace dial." A deterministic,
   documented composite of four components, each scored 0-100 (higher =
   more favorable for deploying cash):
     - ``vix``: 100 minus the VIX's percentile rank within its trailing
       52-week history (high VIX / high percentile = fear = low score).
     - ``term_structure``: VIX3M/VIX contango ratio mapped to a 0-100 score
       (contango = calm = high score; backwardation = stress = low score).
     - ``breadth``: % of S&P 500 constituents trading above their 200-day
       moving average, used directly as the score.
     - ``credit``: 100 minus the ICE BofA HY OAS's percentile rank within
       its trailing 52-week history (wide spreads = stress = low score).
   The composite is a weighted average (default: equal 25% each) computed
   only over the components that were actually available, with weights
   renormalized over the available set — a single flaky free-data source
   degrades the score's precision, not the whole pipeline. This score dials
   *portfolio-level* cash-deployment pace; it is explicitly NOT a single-name
   veto or stop.
2. **Catalyst calendar** — days-to-catalyst countdown per name in
   ``watches.json``, tolerant of both exact dates and coarse ones like
   "2026-Q4" (resolved to quarter-end, flagged with reduced precision).
3. **Prediction-market poll (best-effort)**:
     - TTWO: polls Kalshi's public ``KXGTA6`` series (verified live: a real,
       actively-traded "will GTA6 release by <date>?" ladder) for the market
       whose date threshold most tightly brackets the watch's catalyst date,
       treating its YES price as the "on-time" probability. Stores it to
       ``predmkt_history`` and flags a same-day drop >= the configured
       points threshold. If Kalshi requires auth we don't have configured
       (401/403 with no ``KALSHI_API_KEY``), this logs "check Kalshi
       manually" instead of failing — same for any other best-effort miss
       (network error, no matching market, etc.), each with its own note.
     - WBD: no external prediction-market venue is used. Per the task spec,
       the merger-arb spread ``(deal_price - price) / price`` *is* WBD's
       prediction market. Computed from the latest Layer A snapshot's WBD
       price, stored to ``predmkt_history``, and flagged on widening past
       (break-risk repricing) or converging under (deal closing) the
       configured thresholds.
4. **News scan** — this script does NOT crawl or subscribe to any live news
   API (none is configured in ``.env.example``, and none was requested). It
   defines the per-name keyword sets and a pure ``scan_headlines()`` matcher
   that turns a supplied headline list into a short bulleted digest. Sourcing
   the headlines is left to the operator's Claude Code session (e.g. a
   WebSearch pass, saved to a JSON file and passed via ``--headlines``) — the
   digest is for a human/agent to *review*, never to act on automatically.

Usage:
    python layer_c_macro.py                          # live pull, all parts
    python layer_c_macro.py --asof 2026-06-30         # historical macro/predmkt as-of a date
    python layer_c_macro.py --headlines headlines.json  # include the news digest
    python layer_c_macro.py --skip-breadth            # skip the heavy S&P 500 breadth pull
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

import layer_a_data as la_data
from layer_a_data import days_between, find_latest_asof, parse_date

try:
    from dotenv import load_dotenv
except ImportError:
    def load_dotenv(*_args, **_kwargs):
        return False

WATCHES_FILE = "watches.json"
CONSTITUENTS_CACHE = os.path.join(la_data.DEFAULT_DATA_DIR, "sp500_constituents.json")
CONSTITUENTS_CACHE_MAX_AGE_DAYS = 30

HTTP_TIMEOUT = 20
USER_AGENT = "Mozilla/5.0 (compatible; HavenviewBot/1.0; read-only market monitor)"

# --------------------------------------------------------------------------
# Macro score configuration — documented, deterministic, tunable constants.
# --------------------------------------------------------------------------
FRED_BASE = "https://fred.stlouisfed.org/graph/fredgraph.csv"
FRED_SERIES_VIX = "VIXCLS"       # CBOE Volatility Index
FRED_SERIES_VIX3M = "VXVCLS"     # CBOE S&P 500 3-Month Volatility Index
FRED_SERIES_HY_OAS = "BAMLH0A0HYM2"  # ICE BofA US High Yield Index OAS

PERCENTILE_WINDOW_DAYS = 365  # ~52 weeks of calendar days of history to rank against
TERM_STRUCTURE_RATIO_SCALE = 500.0  # maps VIX3M/VIX ratio deviation from 1.0 to score points

DEFAULT_WEIGHTS = {
    "vix": 25.0,
    "term_structure": 25.0,
    "breadth": 25.0,
    "credit": 25.0,
}

# --------------------------------------------------------------------------
# Prediction-market / DTE-flag defaults (overridden by watches.json tripwires
# when present, so the seed config file stays the single source of truth).
# --------------------------------------------------------------------------
DEFAULT_PREDMKT_DROP_PTS = 8.0
DEFAULT_SPREAD_WIDEN_PCT = 20.0
DEFAULT_SPREAD_CONVERGE_PCT = 4.0

KALSHI_API_BASE = "https://api.elections.kalshi.com/trade-api/v2"
# TTWO-specific: the Kalshi series with a ladder of "will GTA6 release by
# <date>?" markets. Verified live against Kalshi's public API. If Kalshi
# retires/renames this series, this constant is the one place to update it.
KALSHI_TTWO_SERIES = "KXGTA6"

# --------------------------------------------------------------------------
# News-scan keyword sets (see module docstring, part 4).
# --------------------------------------------------------------------------
NEWS_KEYWORDS = {
    "WBD": [
        "state attorney general sue",
        "EU Foreign Subsidies",
        "FCC foreign ownership",
        "deal blocked",
        "merger terminated",
        "merger completed",
    ],
    "TTWO": [
        "GTA VI delay",
        "Rockstar delay",
        "release date pushed",
    ],
    "RKLB": [
        "Neutron delay",
        "Neutron slip",
        "hot fire",
        "maiden flight scrub",
        "anomaly",
    ],
}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def http_get(url: str, params: dict | None = None, headers: dict | None = None, timeout: int = HTTP_TIMEOUT):
    """Thin wrapper so every external call in this module goes through one
    place and picks up a consistent User-Agent / timeout / error surface."""
    import requests

    hdrs = {"User-Agent": USER_AGENT}
    if headers:
        hdrs.update(headers)
    return requests.get(url, params=params, headers=hdrs, timeout=timeout)


# ==========================================================================
# PART 1 — Macro score
# ==========================================================================
def fetch_fred_series(series_id: str) -> list:
    """Full history of a FRED series as [(date_str, value_float), ...],
    ascending by date. FRED's CSV endpoint needs no API key. Raises on any
    HTTP/parse failure — callers decide how to degrade."""
    resp = http_get(FRED_BASE, params={"id": series_id})
    resp.raise_for_status()
    reader = csv.reader(io.StringIO(resp.text))
    header = next(reader)
    if len(header) < 2:
        raise ValueError(f"unexpected FRED CSV header for {series_id}: {header}")
    out = []
    for row in reader:
        if len(row) < 2 or row[1] in ("", "."):
            continue  # FRED marks non-trading/holiday gaps with "."
        out.append((row[0], float(row[1])))
    return out


def percentile_rank(history: list, current: float) -> float:
    """% of ``history`` values <= ``current``. 100 = highest observed."""
    if not history:
        return 50.0  # no basis for comparison — neutral
    return 100.0 * sum(1 for v in history if v <= current) / len(history)


def series_as_of(history: list, asof_date: str, window_days: int = PERCENTILE_WINDOW_DAYS):
    """Filter a (date_str, value) series to observations on/before
    ``asof_date``, and split into (current_value, trailing_window_values).
    Returns (None, []) if there's no observation on/before asof_date.
    """
    cutoff = parse_date(asof_date)
    filtered = [(d, v) for d, v in history if parse_date(d) is not None and parse_date(d) <= cutoff]
    if not filtered:
        return None, []
    current_date, current_value = filtered[-1]
    window_start = cutoff - timedelta(days=window_days)
    trailing = [v for d, v in filtered[:-1] if parse_date(d) >= window_start]
    return current_value, trailing


def compute_vix_component(asof_date: str) -> dict:
    try:
        history = fetch_fred_series(FRED_SERIES_VIX)
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "note": f"VIX pull failed: {exc}"}
    level, trailing = series_as_of(history, asof_date)
    if level is None:
        return {"available": False, "note": "no VIX observation on/before asof"}
    pct = percentile_rank(trailing, level)
    thin = len(trailing) < 60
    score = round(100.0 - pct, 2)
    return {
        "available": True,
        "level": level,
        "pct_52wk": round(pct, 2),
        "n_history": len(trailing),
        "thin_history": thin,
        "score": score,
        "note": "thin trailing history — percentile is low-confidence" if thin else None,
    }


def compute_term_structure_component(asof_date: str) -> dict:
    try:
        vix_hist = fetch_fred_series(FRED_SERIES_VIX)
        vix3m_hist = fetch_fred_series(FRED_SERIES_VIX3M)
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "note": f"term structure pull failed: {exc}"}
    vix, _ = series_as_of(vix_hist, asof_date)
    vix3m, _ = series_as_of(vix3m_hist, asof_date)
    if vix is None or vix3m is None or vix <= 0:
        return {"available": False, "note": "missing VIX/VIX3M observation on/before asof"}
    ratio = vix3m / vix
    regime = "contango" if ratio >= 1.0 else "backwardation"
    score = max(0.0, min(100.0, 50.0 + (ratio - 1.0) * TERM_STRUCTURE_RATIO_SCALE))
    return {
        "available": True,
        "vix": vix,
        "vix3m": vix3m,
        "ratio": round(ratio, 4),
        "regime": regime,
        "score": round(score, 2),
        "note": None,
    }


def compute_credit_component(asof_date: str) -> dict:
    try:
        history = fetch_fred_series(FRED_SERIES_HY_OAS)
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "note": f"HY OAS pull failed: {exc}"}
    level, trailing = series_as_of(history, asof_date)
    if level is None:
        return {"available": False, "note": "no HY OAS observation on/before asof"}
    pct = percentile_rank(trailing, level)
    thin = len(trailing) < 60
    score = round(100.0 - pct, 2)
    return {
        "available": True,
        "oas_level": level,
        "pct_52wk": round(pct, 2),
        "n_history": len(trailing),
        "thin_history": thin,
        "score": score,
        "note": "thin trailing history — percentile is low-confidence" if thin else None,
    }


def load_sp500_constituents(cache_path: str = CONSTITUENTS_CACHE, max_age_days: int = CONSTITUENTS_CACHE_MAX_AGE_DAYS) -> list:
    """S&P 500 tickers, cached locally (Wikipedia rarely changes membership
    day-to-day; no need to re-scrape every run). Raises on total failure
    (no cache and the live scrape also fails) — caller decides how to
    degrade."""
    if os.path.exists(cache_path):
        with open(cache_path) as fh:
            cached = json.load(fh)
        fetched_at = parse_date((cached.get("fetched_at") or "")[:10])
        if fetched_at and (datetime.now(timezone.utc).date() - fetched_at).days <= max_age_days:
            return cached["tickers"]

    import pandas as pd

    resp = http_get("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies")
    resp.raise_for_status()
    tables = pd.read_html(io.StringIO(resp.text))
    tickers = [str(s).replace(".", "-") for s in tables[0]["Symbol"].tolist()]

    os.makedirs(os.path.dirname(cache_path) or ".", exist_ok=True)
    with open(cache_path, "w") as fh:
        json.dump({"fetched_at": utc_now_iso(), "tickers": tickers}, fh, indent=2)
    return tickers


def compute_breadth_component(skip: bool = False) -> dict:
    """% of S&P 500 constituents trading above their 200-day moving average.
    The heaviest, most failure-prone component (500-name batch pull) — wrapped
    to degrade to unavailable rather than take the whole score down with it.
    """
    if skip:
        return {"available": False, "note": "breadth pull skipped (--skip-breadth)"}
    try:
        tickers = load_sp500_constituents()
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "note": f"constituent list unavailable: {exc}"}

    try:
        import yfinance as yf

        session = la_data.build_yf_session()
        data = yf.download(
            tickers, period="1y", session=session, progress=False,
            threads=True, auto_adjust=False, group_by="ticker",
        )
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "note": f"batch price pull failed: {exc}"}

    above = evaluated = 0
    for ticker in tickers:
        try:
            closes = data[ticker]["Close"].dropna()
        except Exception:  # noqa: BLE001 - ticker missing/delisted/renamed
            continue
        if len(closes) < 200:
            continue
        evaluated += 1
        sma200 = closes.tail(200).mean()
        if closes.iloc[-1] > sma200:
            above += 1

    if evaluated == 0:
        return {"available": False, "note": "no constituents had sufficient price history"}

    pct = round(100.0 * above / evaluated, 2)
    coverage_note = None
    if evaluated < 0.8 * len(tickers):
        coverage_note = f"only {evaluated}/{len(tickers)} constituents had usable data"
    return {
        "available": True,
        "pct_above_200dma": pct,
        "n_evaluated": evaluated,
        "n_constituents": len(tickers),
        "score": pct,
        "note": coverage_note,
    }


def compute_macro_score(asof_date: str, weights: dict = None, skip_breadth: bool = False) -> dict:
    """Part 1 entry point: pull all four components and combine into the
    composite deployment-pace dial. Each component degrades independently."""
    weights = weights or DEFAULT_WEIGHTS
    components = {
        "vix": compute_vix_component(asof_date),
        "term_structure": compute_term_structure_component(asof_date),
        "breadth": compute_breadth_component(skip=skip_breadth),
        "credit": compute_credit_component(asof_date),
    }

    available = {k: v for k, v in components.items() if v.get("available")}
    weight_sum = sum(weights.get(k, 0.0) for k in available)
    if weight_sum > 0:
        composite = sum(available[k]["score"] * weights.get(k, 0.0) for k in available) / weight_sum
        composite = round(composite, 2)
    else:
        composite = None

    return {
        "asof": asof_date,
        "composite": composite,
        "data_complete": len(available) == len(components),
        "n_components_available": len(available),
        "n_components_total": len(components),
        "weights": weights,
        "components": components,
        "note": "dials portfolio-level cash-deployment pace; NOT a single-name veto or stop",
    }


def persist_macro_history(conn: sqlite3.Connection, asof_date: str, macro: dict) -> None:
    """Idempotent per-day upsert into macro_history, one row per series."""
    rows = []
    c = macro["components"]
    if c["vix"].get("available"):
        rows += [("VIX_LEVEL", c["vix"]["level"]), ("VIX_PCT_52WK", c["vix"]["pct_52wk"])]
    if c["term_structure"].get("available"):
        rows += [
            ("VIX3M_LEVEL", c["term_structure"]["vix3m"]),
            ("TERM_STRUCTURE_RATIO", c["term_structure"]["ratio"]),
            ("TERM_STRUCTURE_SCORE", c["term_structure"]["score"]),
        ]
    if c["breadth"].get("available"):
        rows.append(("BREADTH_PCT_ABOVE_200DMA", c["breadth"]["pct_above_200dma"]))
    if c["credit"].get("available"):
        rows += [("HY_OAS_LEVEL", c["credit"]["oas_level"]), ("HY_OAS_PCT_52WK", c["credit"]["pct_52wk"])]
    if macro["composite"] is not None:
        rows.append(("MACRO_SCORE", macro["composite"]))

    series_names = [r[0] for r in rows]
    if series_names:
        placeholders = ",".join("?" * len(series_names))
        conn.execute(
            f"DELETE FROM macro_history WHERE date(ts) = ? AND series IN ({placeholders})",
            [asof_date] + series_names,
        )
    ts = utc_now_iso()
    conn.executemany(
        "INSERT INTO macro_history (ts, series, value) VALUES (?, ?, ?)",
        [(ts, series, value) for series, value in rows],
    )


# ==========================================================================
# PART 2 — Catalyst calendar
# ==========================================================================
def load_watches(path: str = WATCHES_FILE) -> list:
    with open(path) as fh:
        return json.load(fh)


def resolve_watch_date(raw: str):
    """Return (resolved_date_str, precision) for a watch's date field.
    Handles exact "YYYY-MM-DD" and coarse "YYYY-QN" (resolved to quarter-end).
    """
    if not raw:
        return None, None
    exact = parse_date(raw)
    if exact:
        return raw, "exact"

    if len(raw) == 7 and raw[4] == "-" and raw[5] == "Q" and raw[6] in "1234":
        year = int(raw[:4])
        quarter = int(raw[6])
        quarter_end_month = quarter * 3
        # last day of quarter_end_month
        if quarter_end_month == 12:
            end = f"{year}-12-31"
        else:
            next_month_first = datetime(year, quarter_end_month + 1, 1).date()
            end = (next_month_first - timedelta(days=1)).isoformat()
        return end, "quarter"

    return None, "unparsed"


def compute_catalyst_calendar(watches: list, asof_date: str) -> list:
    out = []
    for w in watches:
        raw = w.get("date") or w.get("close_target")
        resolved, precision = resolve_watch_date(raw)
        dte = days_between(asof_date, resolved) if resolved else None
        out.append({
            "ticker": w["ticker"],
            "type": w.get("type"),
            "catalyst": w.get("catalyst"),
            "certainty": w.get("certainty"),
            "date_raw": raw,
            "date_resolved": resolved,
            "date_precision": precision,
            "days_to_catalyst": dte,
            "is_past": dte is not None and dte < 0,
        })
    return out


# ==========================================================================
# PART 3 — Prediction-market poll
# ==========================================================================
def fetch_kalshi_markets(series_ticker: str, status: str = "open") -> list:
    headers = {}
    api_key = os.getenv("KALSHI_API_KEY")
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    resp = http_get(
        f"{KALSHI_API_BASE}/markets",
        params={"series_ticker": series_ticker, "status": status, "limit": 100},
        headers=headers,
    )
    if resp.status_code in (401, 403):
        if not api_key:
            raise PermissionError("kalshi_auth_required_no_key")
        raise PermissionError(f"kalshi_auth_rejected_{resp.status_code}")
    resp.raise_for_status()
    return resp.json().get("markets", [])


def select_ontime_market(markets: list, catalyst_date: str):
    """Pick the market whose close date most tightly brackets the catalyst
    date: the smallest close-date that is still >= catalyst_date (so its YES
    price reads as "released by around when we expect it"). Falls back to the
    furthest-out market if every listed market resolves before the catalyst.
    """
    dated = []
    for m in markets:
        close_date = (m.get("close_time") or "")[:10]
        if parse_date(close_date):
            dated.append((close_date, m))
    if not dated:
        return None, "no dated markets in series"

    cutoff = parse_date(catalyst_date)
    on_or_after = sorted((d, m) for d, m in dated if parse_date(d) >= cutoff)
    if on_or_after:
        return on_or_after[0][1], "exact_bracket"

    latest = max(dated, key=lambda dm: parse_date(dm[0]))
    return latest[1], "no_market_covers_catalyst_date_using_furthest_available"


def _dollar(field) -> float | None:
    try:
        return float(field) if field not in (None, "") else None
    except (TypeError, ValueError):
        return None


def poll_kalshi_ttwo(conn: sqlite3.Connection, watch: dict, asof_date: str) -> dict:
    """Best-effort Kalshi poll for TTWO. Never raises — every failure mode
    (auth, network, no matching market) degrades to a note instead."""
    market_id = f"kalshi:{KALSHI_TTWO_SERIES}"
    catalyst_date, _ = resolve_watch_date(watch.get("date"))
    if not catalyst_date:
        return {"status": "skipped", "note": "watch has no resolvable catalyst date"}

    try:
        markets = fetch_kalshi_markets(KALSHI_TTWO_SERIES)
    except PermissionError as exc:
        return {"status": "check_manually", "note": f"Kalshi auth required and unavailable ({exc}) — check Kalshi manually"}
    except Exception as exc:  # noqa: BLE001
        return {"status": "failed", "note": f"Kalshi pull failed ({exc}) — check Kalshi manually"}

    market, selection_note = select_ontime_market(markets, catalyst_date)
    if market is None:
        return {"status": "failed", "note": f"no usable {KALSHI_TTWO_SERIES} market found — check Kalshi manually"}

    yes_bid, yes_ask = _dollar(market.get("yes_bid_dollars")), _dollar(market.get("yes_ask_dollars"))
    yes_price = (
        (yes_bid + yes_ask) / 2 if (yes_bid is not None and yes_ask is not None)
        else _dollar(market.get("last_price_dollars"))
    )
    no_bid, no_ask = _dollar(market.get("no_bid_dollars")), _dollar(market.get("no_ask_dollars"))
    no_price = (no_bid + no_ask) / 2 if (no_bid is not None and no_ask is not None) else None
    volume = _dollar(market.get("volume_fp"))

    if yes_price is None:
        return {"status": "failed", "note": "matched market has no usable price — check Kalshi manually"}

    prior = conn.execute(
        "SELECT yes_price FROM predmkt_history "
        "WHERE market = ? AND date(ts) < ? ORDER BY ts DESC LIMIT 1",
        (market_id, asof_date),
    ).fetchone()
    drop_pts = None
    drop_threshold = (watch.get("tripwires") or {}).get("predmkt_ontime_drop_pts", DEFAULT_PREDMKT_DROP_PTS)
    dropped = False
    if prior and prior[0] is not None:
        drop_pts = round((prior[0] - yes_price) * 100.0, 2)
        dropped = drop_pts >= drop_threshold

    conn.execute("DELETE FROM predmkt_history WHERE market = ? AND date(ts) = ?", (market_id, asof_date))
    conn.execute(
        "INSERT INTO predmkt_history (ts, market, ticker, yes_price, no_price, volume, raw) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (utc_now_iso(), market_id, watch["ticker"], yes_price, no_price, volume, json.dumps(market)),
    )

    return {
        "status": "ok",
        "market": market_id,
        "kalshi_ticker": market.get("ticker"),
        "kalshi_title": market.get("title"),
        "selection": selection_note,
        "yes_price": yes_price,
        "no_price": no_price,
        "volume": volume,
        "drop_pts": drop_pts,
        "drop_threshold": drop_threshold,
        "flag_drop": dropped,
    }


def compute_wbd_arb_spread(conn: sqlite3.Connection, watch: dict, wbd_price, asof_date: str) -> dict:
    """WBD's own price *is* its prediction market (merger-arb spread)."""
    market_id = "wbd:psky_arb_spread"
    deal_price = watch.get("deal_price")
    if deal_price is None or wbd_price is None or wbd_price <= 0:
        return {"status": "unavailable", "note": "missing deal_price or WBD underlying price"}

    spread_pct = round((deal_price - wbd_price) / wbd_price * 100.0, 4)
    widen_threshold = (watch.get("tripwires") or {}).get("spread_widen_pct", DEFAULT_SPREAD_WIDEN_PCT)
    converge_threshold = (watch.get("tripwires") or {}).get("spread_converge_pct", DEFAULT_SPREAD_CONVERGE_PCT)
    widened = spread_pct > widen_threshold
    converged = spread_pct < converge_threshold

    raw = {
        "deal_price": deal_price, "price": wbd_price, "spread_pct": spread_pct,
        "widen_threshold": widen_threshold, "converge_threshold": converge_threshold,
        "widened": widened, "converged": converged,
    }
    conn.execute("DELETE FROM predmkt_history WHERE market = ? AND date(ts) = ?", (market_id, asof_date))
    conn.execute(
        "INSERT INTO predmkt_history (ts, market, ticker, yes_price, no_price, volume, raw) "
        "VALUES (?, ?, ?, NULL, NULL, NULL, ?)",
        (utc_now_iso(), market_id, watch["ticker"], json.dumps(raw)),
    )

    return {"status": "ok", "market": market_id, **raw}


# ==========================================================================
# PART 4 — News scan (matcher only; headline sourcing happens in-session)
# ==========================================================================
def scan_headlines(headlines: list, keywords: dict = NEWS_KEYWORDS) -> dict:
    """Pure text match: no fetching, no action. ``headlines`` is a list of
    strings or {"title": ..., "source": ..., "published_at": ..., "url": ...}
    dicts (as an operator's WebSearch/news pull would naturally produce)."""
    matches = {ticker: [] for ticker in keywords}
    for h in headlines:
        text = h if isinstance(h, str) else " ".join(
            str(h.get(k, "")) for k in ("title", "summary")
        )
        text_lower = text.lower()
        for ticker, kw_list in keywords.items():
            hit_kws = [kw for kw in kw_list if kw.lower() in text_lower]
            if hit_kws:
                entry = {"matched_keywords": hit_kws, "text": text}
                if isinstance(h, dict):
                    entry.update({k: h.get(k) for k in ("url", "source", "published_at") if k in h})
                matches[ticker].append(entry)
    return matches


def format_news_digest(matches: dict) -> str:
    lines = ["### News digest — keyword matches only. Review manually; do not act on this alone."]
    any_hit = False
    for ticker, hits in matches.items():
        if not hits:
            continue
        any_hit = True
        lines.append(f"\n**{ticker}**")
        for h in hits:
            src = f" ({h['source']})" if h.get("source") else ""
            lines.append(f"- \"{h['text']}\"{src} — matched: {', '.join(h['matched_keywords'])}")
    if not any_hit:
        lines.append("\n(no keyword matches in the supplied headlines)")
    return "\n".join(lines)


# ==========================================================================
# Orchestration / CLI
# ==========================================================================
def main():
    parser = argparse.ArgumentParser(description="Havenview Layer C — macro gate, catalysts, prediction markets, news scan (read-only)")
    parser.add_argument("--db", default=la_data.DEFAULT_DB)
    parser.add_argument("--data-dir", default=la_data.DEFAULT_DATA_DIR)
    parser.add_argument("--watches", default=WATCHES_FILE)
    parser.add_argument("--asof", metavar="YYYY-MM-DD", help="Reference date (default: today)")
    parser.add_argument("--skip-breadth", action="store_true", help="Skip the heavy S&P 500 breadth pull")
    parser.add_argument("--headlines", metavar="PATH", help="JSON file of headlines to scan (part 4)")
    parser.add_argument("--w-vix", type=float, default=DEFAULT_WEIGHTS["vix"])
    parser.add_argument("--w-term", type=float, default=DEFAULT_WEIGHTS["term_structure"])
    parser.add_argument("--w-breadth", type=float, default=DEFAULT_WEIGHTS["breadth"])
    parser.add_argument("--w-credit", type=float, default=DEFAULT_WEIGHTS["credit"])
    args = parser.parse_args()

    load_dotenv()
    asof_date = args.asof or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    weights = {"vix": args.w_vix, "term_structure": args.w_term, "breadth": args.w_breadth, "credit": args.w_credit}

    watches = load_watches(args.watches)
    conn = sqlite3.connect(args.db)

    result = {"asof": asof_date, "generated_at": utc_now_iso()}
    try:
        # Part 1
        macro = compute_macro_score(asof_date, weights=weights, skip_breadth=args.skip_breadth)
        persist_macro_history(conn, asof_date, macro)
        result["macro_score"] = macro

        # Part 2
        result["catalyst_calendar"] = compute_catalyst_calendar(watches, asof_date)

        # Part 3
        ttwo_watch = next((w for w in watches if w["ticker"] == "TTWO"), None)
        wbd_watch = next((w for w in watches if w["ticker"] == "WBD"), None)
        predmkt = {}
        if ttwo_watch:
            predmkt["TTWO"] = poll_kalshi_ttwo(conn, ttwo_watch, asof_date)
        if wbd_watch:
            snapshot = la_data.replay(
                find_latest_asof(args.data_dir, args.db) or asof_date, args.data_dir, args.db
            ) if (find_latest_asof(args.data_dir, args.db)) else {"tickers": {}}
            wbd_price = (snapshot.get("tickers", {}).get("WBD") or {}).get("underlying")
            predmkt["WBD"] = compute_wbd_arb_spread(conn, wbd_watch, wbd_price, asof_date)
        result["prediction_markets"] = predmkt
        conn.commit()
    finally:
        conn.close()

    # Part 4
    if args.headlines:
        with open(args.headlines) as fh:
            headlines = json.load(fh)
        matches = scan_headlines(headlines)
        result["news_digest"] = {
            "matches": matches,
            "digest_text": format_news_digest(matches),
        }
    else:
        result["news_digest"] = {
            "note": "no --headlines supplied; source recent headlines in your Claude Code "
                    "session (e.g. a web search) and pass them via --headlines to scan",
            "keywords": NEWS_KEYWORDS,
        }

    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
