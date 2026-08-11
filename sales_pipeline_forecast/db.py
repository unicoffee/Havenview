"""SQLite storage for deals and comp-plan settings.

Everything here is local, personal data: one file, ``pipeline.db``, created
next to this module. There is no network access and no external account
connection — this module only reads/writes its own SQLite file.
"""
import json
import sqlite3
from contextlib import contextmanager
from datetime import date, timedelta
from pathlib import Path

DB_PATH = Path(__file__).parent / "pipeline.db"

# (stage name, default probability %, default forecast category)
STAGES = [
    ("Prospecting", 10, "Pipeline"),
    ("Qualification", 25, "Pipeline"),
    ("Needs Analysis", 40, "Pipeline"),
    ("Proposal", 60, "Best Case"),
    ("Negotiation", 80, "Commit"),
    ("Closed Won", 100, "Closed Won"),
    ("Closed Lost", 0, "Omitted"),
]
STAGE_NAMES = [s[0] for s in STAGES]
STAGE_DEFAULTS = {s[0]: {"probability": s[1], "forecast_category": s[2]} for s in STAGES}

FORECAST_CATEGORIES = ["Pipeline", "Best Case", "Commit", "Closed Won", "Closed Lost", "Omitted"]

DEFAULT_COMP_PLAN = {
    "annual_quota": 1_200_000,
    "annual_base_salary": 70_000,
    "quota_period": "quarterly",  # "monthly" or "quarterly"
    "commission_tiers": [
        {"up_to_pct": 100, "rate_pct": 8},
        {"up_to_pct": 150, "rate_pct": 12},
        {"up_to_pct": None, "rate_pct": 16},
    ],
}


@contextmanager
def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with get_connection() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS deals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                account TEXT NOT NULL DEFAULT '',
                amount REAL NOT NULL,
                stage TEXT NOT NULL,
                probability REAL NOT NULL,
                forecast_category TEXT NOT NULL,
                expected_close_date TEXT NOT NULL,
                created_date TEXT NOT NULL,
                notes TEXT NOT NULL DEFAULT ''
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )


def _sample_deals():
    today = date.today()

    def d(days):
        return (today + timedelta(days=days)).isoformat()

    rows = [
        ("Acme Corp — Platform Renewal", "Acme Corp", 84000, "Negotiation", d(12)),
        ("Globex Expansion", "Globex", 132000, "Proposal", d(28)),
        ("Initech Net-New", "Initech", 46000, "Qualification", d(55)),
        ("Umbrella Upsell", "Umbrella Inc", 61000, "Prospecting", d(80)),
        ("Soylent Multi-Year", "Soylent Corp", 210000, "Negotiation", d(9)),
        ("Hooli Pilot Conversion", "Hooli", 38000, "Needs Analysis", d(40)),
        ("Wayne Ent. Security Add-on", "Wayne Enterprises", 97000, "Closed Won", d(-6)),
        ("Stark Industries — Lost", "Stark Industries", 55000, "Closed Lost", d(-15)),
    ]
    return rows


def seed_if_empty():
    with get_connection() as conn:
        count = conn.execute("SELECT COUNT(*) FROM deals").fetchone()[0]
        if count > 0:
            return
        today = date.today().isoformat()
        for name, account, amount, stage, close_date in _sample_deals():
            defaults = STAGE_DEFAULTS[stage]
            conn.execute(
                """
                INSERT INTO deals
                    (name, account, amount, stage, probability, forecast_category,
                     expected_close_date, created_date, notes)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    name,
                    account,
                    amount,
                    stage,
                    defaults["probability"],
                    defaults["forecast_category"],
                    close_date,
                    today,
                    "",
                ),
            )
        if conn.execute("SELECT COUNT(*) FROM settings WHERE key = 'comp_plan'").fetchone()[0] == 0:
            conn.execute(
                "INSERT INTO settings (key, value) VALUES ('comp_plan', ?)",
                (json.dumps(DEFAULT_COMP_PLAN),),
            )


def list_deals():
    with get_connection() as conn:
        rows = conn.execute("SELECT * FROM deals ORDER BY expected_close_date ASC").fetchall()
        return [dict(r) for r in rows]


def add_deal(name, account, amount, stage, expected_close_date, notes="",
             probability=None, forecast_category=None):
    defaults = STAGE_DEFAULTS.get(stage, {"probability": 50, "forecast_category": "Pipeline"})
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO deals
                (name, account, amount, stage, probability, forecast_category,
                 expected_close_date, created_date, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                name,
                account,
                amount,
                stage,
                probability if probability is not None else defaults["probability"],
                forecast_category or defaults["forecast_category"],
                expected_close_date,
                date.today().isoformat(),
                notes,
            ),
        )


def update_deal(deal_id, **fields):
    if not fields:
        return
    allowed = {"name", "account", "amount", "stage", "probability",
               "forecast_category", "expected_close_date", "notes"}
    cols = [k for k in fields if k in allowed]
    if not cols:
        return
    set_clause = ", ".join(f"{c} = ?" for c in cols)
    values = [fields[c] for c in cols] + [deal_id]
    with get_connection() as conn:
        conn.execute(f"UPDATE deals SET {set_clause} WHERE id = ?", values)


def delete_deal(deal_id):
    with get_connection() as conn:
        conn.execute("DELETE FROM deals WHERE id = ?", (deal_id,))


def replace_all_deals(deal_rows):
    """Used by the Pipeline tab's editable table: full-table upsert by id."""
    with get_connection() as conn:
        existing_ids = {r[0] for r in conn.execute("SELECT id FROM deals").fetchall()}
        seen_ids = set()
        for row in deal_rows:
            deal_id = row.get("id")
            if deal_id and deal_id in existing_ids:
                seen_ids.add(deal_id)
                conn.execute(
                    """
                    UPDATE deals SET name=?, account=?, amount=?, stage=?, probability=?,
                        forecast_category=?, expected_close_date=?, notes=?
                    WHERE id = ?
                    """,
                    (
                        row["name"], row["account"], row["amount"], row["stage"],
                        row["probability"], row["forecast_category"],
                        row["expected_close_date"], row.get("notes", ""), deal_id,
                    ),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO deals
                        (name, account, amount, stage, probability, forecast_category,
                         expected_close_date, created_date, notes)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        row["name"], row["account"], row["amount"], row["stage"],
                        row["probability"], row["forecast_category"],
                        row["expected_close_date"], date.today().isoformat(),
                        row.get("notes", ""),
                    ),
                )
        for stale_id in existing_ids - seen_ids:
            conn.execute("DELETE FROM deals WHERE id = ?", (stale_id,))


def get_comp_plan():
    with get_connection() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = 'comp_plan'").fetchone()
        if row is None:
            return dict(DEFAULT_COMP_PLAN)
        return json.loads(row["value"])


def save_comp_plan(plan: dict):
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO settings (key, value) VALUES ('comp_plan', ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (json.dumps(plan),),
        )
