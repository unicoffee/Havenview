#!/usr/bin/env python3
"""Havenview — Layer B: portfolio analytics.

READ-ONLY, PURE COMPUTE
------------------------
This module reads the latest Layer A snapshot (and ``positions.json`` for
static config: sizing, stops, catalyst dates, sleeves) and produces a compact
analytics dict for the dashboard and the alerter. It performs **no side
effects** of its own: no DB writes, no file writes, no network calls, no
trading of any kind. The core entry point, :func:`compute_portfolio`, is a
pure function — data in, dict out.

What it computes:

1. **Allocation** per position and per sleeve. Allocation is the position's
   configured ``size_pct_reserve`` from ``positions.json`` (the entry-time
   sizing decision) — this skeleton does not track a portfolio NAV/reserve
   dollar figure anywhere, so allocation is the static target weight rather
   than a live mark-to-market percentage. Positions are grouped by their
   ``sleeve`` field, so e.g. TTWO and RKLB (both ``expensive_momentum``) are
   summed into one combined sleeve total and checked against a single
   configurable cap (default 40% of reserve).
2. **Aggregate book Greeks** — net delta, theta/day, vega (and gamma) summed
   across every position's matched legs.
3. **DTE flags**, evaluated per leg against its position's ``catalyst_date``:
   - ``theta_bleed_into_event`` — the leg is approaching its own expiry
     (DTE <= --theta-bleed-dte) and the catalyst it was bought for is still
     ahead AND falls *after* this leg's expiry: theta is decaying the option
     away before the thesis ever gets to play out.
   - ``iv_crush_exposure`` — a short-dated (DTE <= --iv-crush-dte), *single*
     option leg (one-leg position; spreads net their vega and are excluded)
     that is still alive when its catalyst hits: classic binary-event IV
     crush risk.
   - ``expired_leg`` — the leg's expiry has already passed (DTE < 0); a data
     hygiene flag, not a market-risk one.
4. **Stop breaches** — positions whose underlying has hit/crossed
   ``stop_underlying`` (a price on the underlying; breach = spot <= stop).
5. **Per-name heat** — positions whose ``size_pct_reserve`` exceeds a
   configurable per-name cap.

Usage:
    python layer_b_portfolio.py                  # latest snapshot in data/
    python layer_b_portfolio.py --asof 2026-06-30
    python layer_b_portfolio.py --sleeve-cap-pct 35 --name-cap-pct 10
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone

import layer_a_data as la_data
from layer_a_data import days_between, find_latest_asof, parse_date  # noqa: F401 (re-exported)

POSITIONS_FILE = "positions.json"

# Defaults — all overridable via CLI / compute_portfolio() kwargs.
DEFAULT_SLEEVE_CAP_PCT = 40.0
DEFAULT_NAME_CAP_PCT = 15.0
DEFAULT_THETA_BLEED_DTE_DAYS = 45
DEFAULT_IV_CRUSH_DTE_DAYS = 21


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------
# Snapshot discovery / loading (read-only)
# --------------------------------------------------------------------------
def load_snapshot(asof: str | None, data_dir: str, db_path: str) -> dict:
    """Load the snapshot for ``asof`` (or the latest available). Read-only."""
    resolved = asof or find_latest_asof(data_dir, db_path)
    if not resolved:
        raise SystemExit(
            f"No snapshot found in {data_dir} or {db_path}. "
            "Run layer_a_data.py first to collect one."
        )
    return la_data.replay(resolved, data_dir, db_path)


def load_positions(path: str = POSITIONS_FILE) -> list:
    with open(path) as fh:
        return json.load(fh)


# --------------------------------------------------------------------------
# Leg matching: snapshot tickers carry a flat per-ticker leg list (built in
# positions.json order); match each position's legs back to it by identity
# rather than index, so this stays correct even if a ticker ever maps to
# more than one position.
# --------------------------------------------------------------------------
def match_leg(snapshot_legs: list, leg: dict) -> dict | None:
    for snap_leg in snapshot_legs:
        if (
            (snap_leg.get("right") or "").lower() == (leg.get("right") or "").lower()
            and snap_leg.get("strike") == leg.get("strike")
            and snap_leg.get("expiry") == leg.get("expiry")
            and snap_leg.get("qty") == leg.get("qty")
        ):
            return snap_leg
    return None


# --------------------------------------------------------------------------
# DTE flags
# --------------------------------------------------------------------------
def evaluate_dte_flags(
    position: dict,
    asof_date: str,
    theta_bleed_dte_days: int,
    iv_crush_dte_days: int,
) -> list:
    flags = []
    legs = position.get("legs", [])
    is_single_option = len(legs) == 1 and (legs[0].get("right") or "").lower() in ("call", "put")
    catalyst_date = position.get("catalyst_date")
    catalyst_ahead = (
        catalyst_date is not None
        and (d := days_between(asof_date, catalyst_date)) is not None
        and d > 0
    )

    for leg in legs:
        expiry = leg.get("expiry")
        if not expiry:  # shares have no expiry — no DTE risk
            continue
        dte = days_between(asof_date, expiry)
        if dte is None:
            continue

        leg_tag = {
            "ticker": position["ticker"],
            "structure": position["structure"],
            "right": leg.get("right"),
            "strike": leg.get("strike"),
            "expiry": expiry,
            "dte": dte,
            "catalyst_date": catalyst_date,
        }

        if dte < 0:
            flags.append({
                **leg_tag,
                "flag": "expired_leg",
                "severity": "warn",
                "detail": f"leg expiry {expiry} is {-dte}d in the past",
            })
            continue

        if not catalyst_ahead:
            continue  # no pending catalyst — no event-related DTE risk to flag

        catalyst_after_expiry = days_between(expiry, catalyst_date) is not None and (
            days_between(expiry, catalyst_date) > 0
        )

        if catalyst_after_expiry and dte <= theta_bleed_dte_days:
            flags.append({
                **leg_tag,
                "flag": "theta_bleed_into_event",
                "severity": "warn",
                "detail": (
                    f"leg expires in {dte}d but catalyst ({catalyst_date}) is still "
                    "ahead and falls after expiry — theta bleeds out before the "
                    "thesis can play out"
                ),
            })
        elif not catalyst_after_expiry and dte <= iv_crush_dte_days and is_single_option:
            flags.append({
                **leg_tag,
                "flag": "iv_crush_exposure",
                "severity": "warn",
                "detail": (
                    f"single option, {dte}d to expiry, still open through catalyst "
                    f"({catalyst_date}) — binary-event IV-crush risk"
                ),
            })

    return flags


# --------------------------------------------------------------------------
# Core pure-compute function
# --------------------------------------------------------------------------
def compute_portfolio(
    snapshot: dict,
    positions: list,
    sleeve_cap_pct: float = DEFAULT_SLEEVE_CAP_PCT,
    name_cap_pct: float = DEFAULT_NAME_CAP_PCT,
    theta_bleed_dte_days: int = DEFAULT_THETA_BLEED_DTE_DAYS,
    iv_crush_dte_days: int = DEFAULT_IV_CRUSH_DTE_DAYS,
) -> dict:
    """Pure read/compute: snapshot + positions in, analytics dict out.

    No I/O happens in this function — callers (CLI, dashboard, alerter) are
    responsible for loading the snapshot and positions list beforehand.
    """
    asof_date = snapshot.get("asof")
    tickers = snapshot.get("tickers", {})

    positions_out = []
    book_delta = book_gamma = book_theta = book_vega = 0.0
    book_greeks_complete = True
    all_dte_flags = []
    data_warnings = []

    for pos in positions:
        ticker = pos["ticker"]
        snap = tickers.get(ticker)
        missing = snap is None or snap.get("error") is not None
        if missing:
            data_warnings.append({
                "ticker": ticker,
                "reason": snap.get("error") if snap else "ticker absent from snapshot",
            })

        spot = snap.get("underlying") if snap else None
        snap_legs = (snap or {}).get("legs", [])

        # Position-level Greeks/value from THIS position's own legs only
        # (matched by identity, not by ticker-level totals — robust to a
        # ticker someday mapping to more than one position).
        pos_delta = pos_gamma = pos_theta = pos_vega = 0.0
        pos_value = 0.0
        has_greeks = False
        for leg in pos.get("legs", []):
            right = (leg.get("right") or "").lower()
            if right.startswith("share") or right == "":
                if spot is not None:
                    pos_value += spot * leg.get("qty", 0)
                    pos_delta += leg.get("qty", 0)
                continue
            matched = match_leg(snap_legs, leg)
            if matched is None:
                continue
            pos_value += matched.get("value") or 0.0
            g = matched.get("greeks") or {}
            if g.get("delta") is not None:
                has_greeks = True
                pos_delta += g["delta"] * leg.get("qty", 0) * la_data.CONTRACT_MULTIPLIER
                pos_gamma += (g.get("gamma") or 0.0) * leg.get("qty", 0) * la_data.CONTRACT_MULTIPLIER
                pos_theta += (g.get("theta") or 0.0) * leg.get("qty", 0) * la_data.CONTRACT_MULTIPLIER
                pos_vega += (g.get("vega") or 0.0) * leg.get("qty", 0) * la_data.CONTRACT_MULTIPLIER

        if not missing:
            book_delta += pos_delta
            book_gamma += pos_gamma
            book_theta += pos_theta
            book_vega += pos_vega
        else:
            book_greeks_complete = False

        # Stop-underlying breach: downside stop, breach when spot <= stop.
        stop = pos.get("stop_underlying")
        stop_breached = None
        dist_pct = dist_dollar = None
        if stop is not None and spot is not None:
            dist_dollar = round(spot - stop, 4)
            dist_pct = round((dist_dollar / spot) * 100.0, 4) if spot else None
            stop_breached = spot <= stop

        dte_flags = evaluate_dte_flags(
            pos, asof_date, theta_bleed_dte_days, iv_crush_dte_days
        )
        all_dte_flags.extend(dte_flags)

        size_pct_reserve = pos.get("size_pct_reserve")
        positions_out.append({
            "ticker": ticker,
            "structure": pos.get("structure"),
            "sleeve": pos.get("sleeve"),
            "thesis_ref": pos.get("thesis_ref"),
            "catalyst_date": pos.get("catalyst_date"),
            "net_debit": pos.get("net_debit"),
            "size_pct_reserve": size_pct_reserve,
            "name_cap_pct": name_cap_pct,
            "name_cap_breached": (
                size_pct_reserve is not None and size_pct_reserve > name_cap_pct
            ),
            "underlying": spot,
            "position_value": round(pos_value, 2) if pos_value else pos_value,
            "position_greeks": {
                "delta": round(pos_delta, 4) if has_greeks or pos_delta else None,
                "gamma": round(pos_gamma, 6) if has_greeks else None,
                "theta": round(pos_theta, 4) if has_greeks else None,
                "vega": round(pos_vega, 4) if has_greeks else None,
            },
            "stop_underlying": stop,
            "stop_distance_pct": dist_pct,
            "stop_distance_dollar": dist_dollar,
            "stop_breached": stop_breached,
            "targets": pos.get("targets"),
            "dte_flags": dte_flags,
            "data_missing": missing,
        })

    # Sleeve rollup: group by sleeve, sum size_pct_reserve, compare to cap.
    # TTWO + RKLB share sleeve "expensive_momentum" so they land in the same
    # bucket and are checked against the cap together.
    sleeves = {}
    for p in positions_out:
        sleeve = p["sleeve"] or "unassigned"
        bucket = sleeves.setdefault(sleeve, {"tickers": [], "total_size_pct_reserve": 0.0})
        bucket["tickers"].append(p["ticker"])
        bucket["total_size_pct_reserve"] += p["size_pct_reserve"] or 0.0
    for sleeve, bucket in sleeves.items():
        bucket["total_size_pct_reserve"] = round(bucket["total_size_pct_reserve"], 4)
        bucket["cap_pct"] = sleeve_cap_pct
        bucket["breached"] = bucket["total_size_pct_reserve"] > sleeve_cap_pct

    stop_breaches = [p for p in positions_out if p["stop_breached"]]
    name_cap_breaches = [p for p in positions_out if p["name_cap_breached"]]
    sleeve_breaches = [
        {"sleeve": s, **b} for s, b in sleeves.items() if b["breached"]
    ]

    return {
        "asof": asof_date,
        "generated_at": utc_now_iso(),
        "config": {
            "sleeve_cap_pct": sleeve_cap_pct,
            "name_cap_pct": name_cap_pct,
            "theta_bleed_dte_days": theta_bleed_dte_days,
            "iv_crush_dte_days": iv_crush_dte_days,
        },
        "positions": positions_out,
        "sleeves": sleeves,
        "book_greeks": {
            "delta": round(book_delta, 4),
            "gamma": round(book_gamma, 6),
            "theta": round(book_theta, 4),
            "vega": round(book_vega, 4),
            "complete": book_greeks_complete,
        },
        "stop_breaches": stop_breaches,
        "name_cap_breaches": name_cap_breaches,
        "sleeve_breaches": sleeve_breaches,
        "dte_flags": all_dte_flags,
        "data_warnings": data_warnings,
        "has_alerts": bool(
            stop_breaches or name_cap_breaches or sleeve_breaches or all_dte_flags
        ),
    }


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Havenview Layer B — portfolio analytics (read-only)")
    parser.add_argument("--db", default=la_data.DEFAULT_DB, help="SQLite database path")
    parser.add_argument("--data-dir", default=la_data.DEFAULT_DATA_DIR, help="Snapshot directory")
    parser.add_argument("--positions", default=POSITIONS_FILE, help="positions.json path")
    parser.add_argument("--asof", metavar="YYYY-MM-DD", help="Use a specific snapshot (default: latest)")
    parser.add_argument("--sleeve-cap-pct", type=float, default=DEFAULT_SLEEVE_CAP_PCT)
    parser.add_argument("--name-cap-pct", type=float, default=DEFAULT_NAME_CAP_PCT)
    parser.add_argument("--theta-bleed-dte", type=int, default=DEFAULT_THETA_BLEED_DTE_DAYS)
    parser.add_argument("--iv-crush-dte", type=int, default=DEFAULT_IV_CRUSH_DTE_DAYS)
    args = parser.parse_args()

    snapshot = load_snapshot(args.asof, args.data_dir, args.db)
    positions = load_positions(args.positions)

    result = compute_portfolio(
        snapshot,
        positions,
        sleeve_cap_pct=args.sleeve_cap_pct,
        name_cap_pct=args.name_cap_pct,
        theta_bleed_dte_days=args.theta_bleed_dte,
        iv_crush_dte_days=args.iv_crush_dte,
    )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
