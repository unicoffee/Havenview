#!/usr/bin/env python3
"""Initialize the Havenview SQLite database.

Read-only monitor scope: this script only creates local tables used to
record observations and alerts. It NEVER connects to a brokerage and NEVER
places trades. Running it is idempotent — existing tables are left intact.

Usage:
    python init_db.py [--db monitor.db]
"""

import argparse
import sqlite3

SCHEMA = """
-- One row per ticker per polling cycle: the observed market state.
CREATE TABLE IF NOT EXISTS snapshots (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            TEXT    NOT NULL,          -- ISO-8601 UTC capture time
    ticker        TEXT    NOT NULL,
    underlying    REAL,                      -- last/underlying price
    bid           REAL,
    ask           REAL,
    mark          REAL,                      -- position mark (premium)
    iv            REAL,                      -- implied vol (decimal)
    iv_rank       REAL,                      -- 0-100 percentile rank
    delta         REAL,
    gamma         REAL,
    theta         REAL,
    vega          REAL,
    raw           TEXT,                      -- JSON blob of source payload
    created_at    TEXT    DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_snapshots_ticker_ts ON snapshots(ticker, ts);

-- Implied-volatility history, kept separately for IV-rank computation.
CREATE TABLE IF NOT EXISTS iv_history (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            TEXT    NOT NULL,
    ticker        TEXT    NOT NULL,
    iv            REAL,
    iv_rank       REAL,
    hv            REAL,                      -- realized/historical vol
    created_at    TEXT    DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_iv_history_ticker_ts ON iv_history(ticker, ts);

-- Macro / market-context series (e.g. VIX, rates, index levels).
CREATE TABLE IF NOT EXISTS macro_history (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            TEXT    NOT NULL,
    series        TEXT    NOT NULL,          -- e.g. 'VIX', 'US10Y', 'SPX'
    value         REAL,
    created_at    TEXT    DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_macro_history_series_ts ON macro_history(series, ts);

-- Prediction-market quotes (read-only) tied to a catalyst watch.
CREATE TABLE IF NOT EXISTS predmkt_history (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            TEXT    NOT NULL,
    market        TEXT    NOT NULL,          -- e.g. 'kalshi:gta-vi-release'
    ticker        TEXT,                      -- associated watch ticker
    yes_price     REAL,                      -- implied probability 0-1
    no_price      REAL,
    volume        REAL,
    raw           TEXT,                      -- JSON blob of source payload
    created_at    TEXT    DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_predmkt_history_market_ts ON predmkt_history(market, ts);

-- Alerts emitted when a tripwire fires. Alerts-out only.
CREATE TABLE IF NOT EXISTS alerts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            TEXT    NOT NULL,
    ticker        TEXT,
    severity      TEXT,                      -- 'info' | 'warn' | 'critical'
    tripwire      TEXT,                      -- which rule fired
    message       TEXT,
    context       TEXT,                      -- JSON blob with supporting data
    delivered     INTEGER DEFAULT 0,         -- 1 once pushed to webhook
    created_at    TEXT    DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_alerts_ticker_ts ON alerts(ticker, ts);
"""


def init_db(path: str = "monitor.db") -> None:
    conn = sqlite3.connect(path)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
        tables = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            )
        ]
        print(f"Initialized {path} with tables: {', '.join(tables)}")
    finally:
        conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Initialize Havenview SQLite DB")
    parser.add_argument("--db", default="monitor.db", help="Path to SQLite database")
    args = parser.parse_args()
    init_db(args.db)
