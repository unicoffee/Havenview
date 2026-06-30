#!/usr/bin/env python3
"""Havenview — Layer A: market-data collection.

READ-ONLY GUARANTEE
-------------------
This module is *data-in, alerts-out only*. It pulls public market data and
writes a dated JSON snapshot plus rows into the local SQLite database. It
NEVER connects to a brokerage to trade and NEVER places, modifies, or cancels
an order. The optional ``--sync-rh`` flag reads Robinhood *positions only*
(no order endpoints are ever imported or called).

What it does, per ticker found in ``positions.json`` and ``watches.json``:

1. Pull the underlying price and recent history via yfinance.
2. For each option leg, pull the contract from the yfinance option chain
   (impliedVolatility, lastPrice, bid/ask) and compute the Greeks LOCALLY
   with Black-Scholes (scipy ``norm``): delta, gamma, theta, vega — using
   spot, strike, time-to-expiry, the chain IV, and a risk-free-rate constant.
   No paid options feed is used.
3. Compute an IV-rank per name: append today's ATM IV to ``iv_history`` and
   rank it against the trailing window. When history is too thin to be
   meaningful, that is flagged in the output, and the rank is seeded from
   trailing realized volatility until real history fills in.
4. Evaluate ``stop_underlying`` for each position and store the distance of
   the underlying from its stop (both % and $).
5. Write a dated JSON snapshot to ``data/`` and upsert into ``snapshots``.

Replay:
    --asof YYYY-MM-DD   Re-load a stored snapshot from ``data/`` (or the DB)
                        instead of doing any live pulls. Purely read-only;
                        it does not touch the network or mutate the DB.

Greek conventions:
    delta  per $1 move in the underlying, per share (calls 0..1, puts -1..0)
    gamma  delta change per $1 move, per share
    theta  per CALENDAR day (annual theta / 365), per share
    vega   per 1 percentage-point (1%) change in IV, per share
Position-level Greeks scale each leg by qty * 100 (the contract multiplier);
shares contribute delta = qty and zero gamma/theta/vega.

Usage:
    python layer_a_data.py                 # live pull for all tickers
    python layer_a_data.py --asof 2026-06-30
    python layer_a_data.py --sync-rh       # also read RH positions (read-only)
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
from datetime import datetime, timezone

import numpy as np
from scipy.stats import norm

try:
    from dotenv import load_dotenv
except ImportError:  # python-dotenv is optional at runtime
    def load_dotenv(*_args, **_kwargs):
        return False


# --------------------------------------------------------------------------
# Configuration constants
# --------------------------------------------------------------------------
DEFAULT_DB = "monitor.db"
DEFAULT_DATA_DIR = "data"
POSITIONS_FILE = "positions.json"
WATCHES_FILE = "watches.json"

# Annualized risk-free rate used in Black-Scholes. A constant by design — this
# is a monitor, not a pricer; small rate errors are immaterial to the alerts.
RISK_FREE_RATE = 0.043

# IV-rank trailing window (calendar/observation days) and the minimum number of
# stored observations before the rank is considered statistically meaningful.
IV_RANK_WINDOW = 252
MIN_IV_HISTORY = 20

# Trading days per year for annualizing realized volatility.
TRADING_DAYS = 252

CONTRACT_MULTIPLIER = 100


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------
# Black-Scholes Greeks (computed locally; no paid feed)
# --------------------------------------------------------------------------
def black_scholes_greeks(
    spot: float,
    strike: float,
    t_years: float,
    iv: float,
    right: str,
    r: float = RISK_FREE_RATE,
) -> dict:
    """Return {delta, gamma, theta, vega} for one option, per share.

    ``right`` is "call" or "put". Degenerate inputs (non-positive spot/strike,
    expiry, or IV) fall back to the intrinsic-value limit so the pipeline never
    crashes on a bad or expired contract.
    """
    right = (right or "").lower()
    is_call = right.startswith("c")

    if spot is None or strike is None or iv is None:
        return {"delta": None, "gamma": None, "theta": None, "vega": None}

    # Expired / zero-vol: collapse to intrinsic-value Greeks.
    if t_years <= 0 or iv <= 0 or spot <= 0 or strike <= 0:
        if is_call:
            delta = 1.0 if spot > strike else 0.0
        else:
            delta = -1.0 if spot < strike else 0.0
        return {"delta": delta, "gamma": 0.0, "theta": 0.0, "vega": 0.0}

    sqrt_t = math.sqrt(t_years)
    d1 = (math.log(spot / strike) + (r + 0.5 * iv * iv) * t_years) / (iv * sqrt_t)
    d2 = d1 - iv * sqrt_t
    pdf_d1 = norm.pdf(d1)

    if is_call:
        delta = norm.cdf(d1)
        theta_annual = (
            -(spot * pdf_d1 * iv) / (2 * sqrt_t)
            - r * strike * math.exp(-r * t_years) * norm.cdf(d2)
        )
    else:
        delta = norm.cdf(d1) - 1.0
        theta_annual = (
            -(spot * pdf_d1 * iv) / (2 * sqrt_t)
            + r * strike * math.exp(-r * t_years) * norm.cdf(-d2)
        )

    gamma = pdf_d1 / (spot * iv * sqrt_t)
    vega_per_1pt = spot * pdf_d1 * sqrt_t / 100.0  # per 1% change in IV
    theta_per_day = theta_annual / 365.0

    return {
        "delta": float(delta),
        "gamma": float(gamma),
        "theta": float(theta_per_day),
        "vega": float(vega_per_1pt),
    }


def year_fraction(asof_date: str, expiry: str) -> float:
    """Years from ``asof_date`` to ``expiry`` (both YYYY-MM-DD), ACT/365."""
    if not expiry:
        return 0.0
    a = datetime.strptime(asof_date, "%Y-%m-%d").date()
    e = datetime.strptime(expiry, "%Y-%m-%d").date()
    return max((e - a).days, 0) / 365.0


def realized_vol(closes) -> float | None:
    """Annualized close-to-close realized volatility from a price series."""
    arr = np.asarray([c for c in closes if c is not None and c > 0], dtype=float)
    if arr.size < 3:
        return None
    log_rets = np.diff(np.log(arr))
    if log_rets.size < 2:
        return None
    sd = float(np.std(log_rets, ddof=1))
    return sd * math.sqrt(TRADING_DAYS)


# --------------------------------------------------------------------------
# Market-data providers (live = yfinance; the compute layer is provider-
# agnostic so it can be replayed or unit-tested without the network)
# --------------------------------------------------------------------------
def build_yf_session():
    """A plain requests session that trusts the agent-proxy CA bundle.

    yfinance defaults to curl_cffi, whose TLS impersonation does not negotiate
    cleanly through the re-terminating proxy; a stock requests session honoring
    REQUESTS_CA_BUNDLE / SSL_CERT_FILE works. Returns None if requests is
    unavailable so yfinance can fall back to its own default.
    """
    try:
        import requests
    except ImportError:
        return None
    session = requests.Session()
    ca = os.getenv("REQUESTS_CA_BUNDLE") or os.getenv("SSL_CERT_FILE")
    if ca:
        session.verify = ca
    return session


class LiveProvider:
    """Pulls underlying + option-chain data from yfinance."""

    def __init__(self):
        import yfinance as yf  # imported lazily; only needed for live pulls

        self._yf = yf
        self._session = build_yf_session()
        self._chain_cache: dict = {}

    def _ticker(self, ticker: str):
        return self._yf.Ticker(ticker, session=self._session)

    def get_underlying(self, ticker: str) -> dict:
        t = self._ticker(ticker)
        hist = t.history(period="1y", auto_adjust=False)
        closes = (
            [float(x) for x in hist["Close"].tolist()] if not hist.empty else []
        )
        price = closes[-1] if closes else None
        # Prefer an explicit fast/last price when available.
        try:
            fast = t.fast_info
            lp = fast.get("last_price") if hasattr(fast, "get") else fast["lastPrice"]
            if lp:
                price = float(lp)
        except Exception:
            pass
        return {"price": price, "closes": closes}

    def get_expiries(self, ticker: str) -> list:
        try:
            return list(self._ticker(ticker).options)
        except Exception:
            return []

    def get_chain(self, ticker: str, expiry: str) -> dict | None:
        key = (ticker, expiry)
        if key in self._chain_cache:
            return self._chain_cache[key]
        try:
            chain = self._ticker(ticker).option_chain(expiry)
            out = {
                "call": _df_to_records(chain.calls),
                "put": _df_to_records(chain.puts),
            }
        except Exception:
            out = None
        self._chain_cache[key] = out
        return out


def _df_to_records(df) -> list:
    """Subset of option-chain columns we use, as plain dicts."""
    cols = ["strike", "impliedVolatility", "lastPrice", "bid", "ask"]
    keep = [c for c in cols if c in df.columns]
    records = []
    for _, row in df[keep].iterrows():
        rec = {}
        for c in keep:
            v = row[c]
            rec[c] = None if v is None or (isinstance(v, float) and math.isnan(v)) else float(v)
        records.append(rec)
    return records


def find_contract(chain: dict | None, right: str, strike: float) -> dict | None:
    """Exact-strike contract lookup within a chain side."""
    if not chain:
        return None
    side = "call" if right.lower().startswith("c") else "put"
    records = chain.get(side) or []
    best = None
    for rec in records:
        if rec.get("strike") is None:
            continue
        if abs(rec["strike"] - strike) < 1e-6:
            return rec
        # keep nearest as a fallback for logging, but only exact matches return
    return best


def atm_iv_from_chain(chain: dict | None, spot: float) -> float | None:
    """ATM implied vol: average the call & put IV at the strike nearest spot."""
    if not chain or spot is None:
        return None
    ivs = []
    for side in ("call", "put"):
        recs = [r for r in (chain.get(side) or []) if r.get("strike") is not None]
        if not recs:
            continue
        nearest = min(recs, key=lambda r: abs(r["strike"] - spot))
        iv = nearest.get("impliedVolatility")
        if iv and iv > 0:
            ivs.append(iv)
    if not ivs:
        return None
    return float(sum(ivs) / len(ivs))


# --------------------------------------------------------------------------
# IV-rank with realized-vol seeding
# --------------------------------------------------------------------------
def compute_iv_rank(
    conn: sqlite3.Connection,
    ticker: str,
    today_iv: float | None,
    rvol: float | None,
    asof_date: str,
    window: int = IV_RANK_WINDOW,
) -> dict:
    """Percentile rank of today's ATM IV against the trailing window.

    Returns {iv_rank, n_history, meaningful, note}. When fewer than
    ``MIN_IV_HISTORY`` real observations exist, the comparison set is seeded
    with the trailing realized vol so a rank is still defined, and ``meaningful``
    is False with an explanatory note.
    """
    if today_iv is None:
        return {
            "iv_rank": None,
            "n_history": 0,
            "meaningful": False,
            "note": "no ATM IV available for this name",
        }

    rows = conn.execute(
        "SELECT iv FROM iv_history "
        "WHERE ticker = ? AND iv IS NOT NULL AND date(ts) < ? "
        "ORDER BY ts DESC LIMIT ?",
        (ticker, asof_date, window),
    ).fetchall()
    history = [r[0] for r in rows]
    n = len(history)
    meaningful = n >= MIN_IV_HISTORY

    if meaningful:
        sample = history
        note = f"ranked against {n} trailing observations"
    else:
        seed = [rvol] if rvol else []
        sample = history + seed
        if sample:
            note = (
                f"history thin ({n} of {MIN_IV_HISTORY} needed); "
                "rank seeded from trailing realized vol"
            )
        else:
            note = "no IV history and no realized vol; rank not computable yet"

    if not sample:
        return {
            "iv_rank": None,
            "n_history": n,
            "meaningful": False,
            "note": note,
        }

    rank = 100.0 * sum(1 for v in sample if v <= today_iv) / len(sample)
    return {
        "iv_rank": round(rank, 2),
        "n_history": n,
        "meaningful": meaningful,
        "note": note,
    }


# --------------------------------------------------------------------------
# Per-ticker snapshot computation
# --------------------------------------------------------------------------
def compute_ticker(
    conn: sqlite3.Connection,
    provider,
    ticker: str,
    positions: list,
    watches: list,
    asof_date: str,
) -> dict:
    """Build the full computed snapshot dict for one ticker."""
    und = provider.get_underlying(ticker)
    spot = und.get("price")
    rvol = realized_vol(und.get("closes") or [])

    # ATM IV from the nearest expiry chain.
    atm_iv = None
    expiries = provider.get_expiries(ticker)
    if expiries and spot:
        atm_iv = atm_iv_from_chain(provider.get_chain(ticker, expiries[0]), spot)

    # Per-leg contracts + locally-computed Greeks. Aggregate to position level.
    leg_details = []
    pos_delta = pos_gamma = pos_theta = pos_vega = 0.0
    pos_value = 0.0
    has_greeks = False

    for pos in positions:
        for leg in pos.get("legs", []):
            right = (leg.get("right") or "").lower()
            qty = leg.get("qty", 0)

            if right.startswith("share") or right == "":
                price = spot
                value = (price or 0.0) * qty
                pos_value += value
                pos_delta += qty  # 1 delta per share
                leg_details.append({
                    "structure": pos.get("structure"),
                    "right": "shares", "strike": None, "expiry": None,
                    "qty": qty, "iv": None, "last": price,
                    "bid": None, "ask": None, "mark": price,
                    "value": value, "greeks": None,
                })
                continue

            strike = leg.get("strike")
            expiry = leg.get("expiry")
            chain = provider.get_chain(ticker, expiry) if expiry else None
            contract = find_contract(chain, right, strike) if chain else None

            iv = contract.get("impliedVolatility") if contract else None
            bid = contract.get("bid") if contract else None
            ask = contract.get("ask") if contract else None
            last = contract.get("lastPrice") if contract else None
            mid = (bid + ask) / 2 if (bid and ask) else last
            t_years = year_fraction(asof_date, expiry)

            greeks = black_scholes_greeks(spot, strike, t_years, iv, right)
            if greeks.get("delta") is not None:
                has_greeks = True
                mult = qty * CONTRACT_MULTIPLIER
                pos_delta += greeks["delta"] * mult
                pos_gamma += (greeks["gamma"] or 0.0) * mult
                pos_theta += (greeks["theta"] or 0.0) * mult
                pos_vega += (greeks["vega"] or 0.0) * mult

            value = (mid or 0.0) * qty * CONTRACT_MULTIPLIER
            pos_value += value
            leg_details.append({
                "structure": pos.get("structure"),
                "right": right, "strike": strike, "expiry": expiry,
                "qty": qty, "iv": iv, "last": last,
                "bid": bid, "ask": ask, "mark": mid,
                "value": value, "t_years": round(t_years, 4),
                "greeks": greeks,
                "contract_found": contract is not None,
            })

    # IV-rank (records today's ATM IV into iv_history below).
    ivr = compute_iv_rank(conn, ticker, atm_iv, rvol, asof_date)

    # Stop-distance evaluation per position.
    stops = []
    for pos in positions:
        stop = pos.get("stop_underlying")
        if stop is None or spot is None:
            continue
        dist_dollar = spot - stop
        dist_pct = (dist_dollar / spot) * 100.0 if spot else None
        stops.append({
            "structure": pos.get("structure"),
            "sleeve": pos.get("sleeve"),
            "stop_underlying": stop,
            "distance_dollar": round(dist_dollar, 4),
            "distance_pct": round(dist_pct, 4) if dist_pct is not None else None,
            "triggered": spot <= stop,
        })

    return {
        "ticker": ticker,
        "asof": asof_date,
        "captured_at": utc_now_iso(),
        "underlying": spot,
        "realized_vol": round(rvol, 4) if rvol is not None else None,
        "atm_iv": round(atm_iv, 4) if atm_iv is not None else None,
        "iv_rank": ivr,
        "position_greeks": {
            "delta": round(pos_delta, 4) if (has_greeks or pos_delta) else None,
            "gamma": round(pos_gamma, 6) if has_greeks else None,
            "theta": round(pos_theta, 4) if has_greeks else None,
            "vega": round(pos_vega, 4) if has_greeks else None,
        },
        "position_value": round(pos_value, 2) if leg_details else None,
        "legs": leg_details,
        "stops": stops,
        "n_positions": len(positions),
        "n_watches": len(watches),
        "is_watch_only": len(positions) == 0,
    }


# --------------------------------------------------------------------------
# Persistence: iv_history append + snapshots upsert + dated JSON
# --------------------------------------------------------------------------
def persist_iv_history(conn, ticker, snap, asof_date):
    ivr = snap["iv_rank"]
    # Idempotent for re-runs of the same asof date.
    conn.execute(
        "DELETE FROM iv_history WHERE ticker = ? AND date(ts) = ?",
        (ticker, asof_date),
    )
    conn.execute(
        "INSERT INTO iv_history (ts, ticker, iv, iv_rank, hv) VALUES (?,?,?,?,?)",
        (
            snap["captured_at"], ticker, snap["atm_iv"],
            ivr.get("iv_rank"), snap["realized_vol"],
        ),
    )


def upsert_snapshot(conn, ticker, snap, asof_date):
    pg = snap["position_greeks"]
    # Net bid/ask across legs (signed by qty) for a position-level quote.
    net_bid = net_ask = None
    bids = [l["bid"] * l["qty"] for l in snap["legs"] if l.get("bid") is not None]
    asks = [l["ask"] * l["qty"] for l in snap["legs"] if l.get("ask") is not None]
    if bids:
        net_bid = round(sum(bids), 4)
    if asks:
        net_ask = round(sum(asks), 4)

    conn.execute(
        "DELETE FROM snapshots WHERE ticker = ? AND date(ts) = ?",
        (ticker, asof_date),
    )
    conn.execute(
        "INSERT INTO snapshots "
        "(ts, ticker, underlying, bid, ask, mark, iv, iv_rank, "
        " delta, gamma, theta, vega, raw) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            snap["captured_at"], ticker, snap["underlying"],
            net_bid, net_ask, snap["position_value"],
            snap["atm_iv"], snap["iv_rank"].get("iv_rank"),
            pg["delta"], pg["gamma"], pg["theta"], pg["vega"],
            json.dumps(snap),
        ),
    )


def write_dated_snapshot(data_dir, asof_date, payload):
    os.makedirs(data_dir, exist_ok=True)
    path = os.path.join(data_dir, f"{asof_date}.json")
    with open(path, "w") as fh:
        json.dump(payload, fh, indent=2)
    return path


# --------------------------------------------------------------------------
# Replay (--asof): reload a stored snapshot; no network, no DB writes
# --------------------------------------------------------------------------
def replay(asof_date, data_dir, db_path):
    path = os.path.join(data_dir, f"{asof_date}.json")
    if os.path.exists(path):
        with open(path) as fh:
            payload = json.load(fh)
        print(f"[replay] loaded {path}")
        return payload

    # Fall back to reconstructing from the snapshots table's raw blobs.
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT ticker, raw FROM snapshots WHERE date(ts) = ? ORDER BY ticker",
            (asof_date,),
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        raise SystemExit(
            f"[replay] no stored snapshot for {asof_date} "
            f"(looked in {path} and the snapshots table)"
        )
    tickers = {}
    for ticker, raw in rows:
        tickers[ticker] = json.loads(raw) if raw else {"ticker": ticker}
    print(f"[replay] reconstructed {len(tickers)} ticker(s) from DB for {asof_date}")
    return {"asof": asof_date, "source": "db", "tickers": tickers}


# --------------------------------------------------------------------------
# Optional read-only Robinhood position sync (--sync-rh)
# --------------------------------------------------------------------------
def read_robinhood_positions() -> list | None:
    """Read open Robinhood positions ONLY. Never places orders.

    Credentials come from the environment (.env): RH_USERNAME, RH_PASSWORD,
    and optional RH_MFA. No order-entry functions are imported or called.
    Returns a list of position dicts, or None if unavailable.
    """
    try:
        import robin_stocks.robinhood as rh
    except ImportError:
        print("[sync-rh] robin_stocks not installed; skipping (pip install robin_stocks)")
        return None

    user = os.getenv("RH_USERNAME")
    pw = os.getenv("RH_PASSWORD")
    mfa = os.getenv("RH_MFA")
    if not user or not pw:
        print("[sync-rh] RH_USERNAME/RH_PASSWORD not set in .env; skipping")
        return None

    print("[sync-rh] logging in READ-ONLY (positions only; no order access)")
    try:
        rh.login(username=user, password=pw, mfa_code=mfa, store_session=False)
    except Exception as exc:  # noqa: BLE001 - report and continue
        print(f"[sync-rh] login failed: {exc}")
        return None

    out = []
    try:
        for p in rh.account.get_open_stock_positions() or []:
            qty = float(p.get("quantity", 0) or 0)
            if qty == 0:
                continue
            sym = rh.stocks.get_symbol_by_url(p["instrument"])
            out.append({"kind": "shares", "ticker": sym, "qty": qty})
    except Exception as exc:  # noqa: BLE001
        print(f"[sync-rh] could not read stock positions: {exc}")
    try:
        for o in rh.options.get_open_option_positions() or []:
            out.append({
                "kind": "option",
                "ticker": o.get("chain_symbol"),
                "qty": float(o.get("quantity", 0) or 0),
                "option_id": o.get("option_id"),
            })
    except Exception as exc:  # noqa: BLE001
        print(f"[sync-rh] could not read option positions: {exc}")

    print(f"[sync-rh] read {len(out)} open position(s) (display only; "
          "positions.json remains the source of truth)")
    return out


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------
def load_json(path):
    with open(path) as fh:
        return json.load(fh)


def collect_tickers(positions, watches):
    """ticker -> (positions_for_ticker, watches_for_ticker), de-duplicated."""
    mapping: dict = {}
    for p in positions:
        mapping.setdefault(p["ticker"], ([], []))[0].append(p)
    for w in watches:
        mapping.setdefault(w["ticker"], ([], []))[1].append(w)
    return mapping


def run_live(args):
    load_dotenv()
    asof_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    positions = load_json(POSITIONS_FILE)
    watches = load_json(WATCHES_FILE)

    if args.sync_rh:
        rh_positions = read_robinhood_positions()
        if rh_positions:
            print("[sync-rh] broker positions (read-only):")
            for rp in rh_positions:
                print(f"    {rp}")

    provider = LiveProvider()
    conn = sqlite3.connect(args.db)

    mapping = collect_tickers(positions, watches)
    tickers_out = {}
    try:
        for ticker, (pos, wat) in sorted(mapping.items()):
            try:
                snap = compute_ticker(conn, provider, ticker, pos, wat, asof_date)
            except Exception as exc:  # noqa: BLE001 - one bad name shouldn't abort
                print(f"[{ticker}] ERROR during pull: {exc}")
                snap = {"ticker": ticker, "asof": asof_date, "error": str(exc)}
                tickers_out[ticker] = snap
                continue
            persist_iv_history(conn, ticker, snap, asof_date)
            upsert_snapshot(conn, ticker, snap, asof_date)
            tickers_out[ticker] = snap
            _print_ticker_line(snap)
        conn.commit()
    finally:
        conn.close()

    payload = {
        "asof": asof_date,
        "generated_at": utc_now_iso(),
        "risk_free_rate": RISK_FREE_RATE,
        "iv_rank_window": IV_RANK_WINDOW,
        "tickers": tickers_out,
    }
    path = write_dated_snapshot(args.data_dir, asof_date, payload)
    print(f"\nWrote snapshot -> {path}")
    print(f"Upserted {len(tickers_out)} ticker(s) into {args.db} (snapshots, iv_history)")
    return payload


def _print_ticker_line(snap):
    if snap.get("error"):
        return
    ivr = snap.get("iv_rank", {})
    rank = ivr.get("iv_rank")
    flag = "" if ivr.get("meaningful") else " (thin)"
    stops = snap.get("stops", [])
    stop_txt = ""
    if stops:
        s0 = stops[0]
        stop_txt = f" stop {s0['stop_underlying']} ({s0['distance_pct']:+.1f}%)"
        if s0["triggered"]:
            stop_txt += " !TRIGGERED"
    rank_txt = f"{rank}{flag}" if rank is not None else "n/a"
    print(
        f"[{snap['ticker']}] und={snap.get('underlying')} "
        f"atm_iv={snap.get('atm_iv')} ivrank={rank_txt}{stop_txt}"
    )


def main():
    parser = argparse.ArgumentParser(description="Havenview Layer A — market data (read-only)")
    parser.add_argument("--db", default=DEFAULT_DB, help="SQLite database path")
    parser.add_argument("--data-dir", default=DEFAULT_DATA_DIR, help="Snapshot output dir")
    parser.add_argument("--asof", metavar="YYYY-MM-DD",
                        help="Replay a stored snapshot instead of live pulls")
    parser.add_argument("--sync-rh", action="store_true",
                        help="Also read Robinhood positions (READ-ONLY; off by default)")
    args = parser.parse_args()

    if args.asof:
        payload = replay(args.asof, args.data_dir, args.db)
        print(json.dumps(payload, indent=2))
        return

    run_live(args)


if __name__ == "__main__":
    main()
