"""SQLite access + schema. Stdlib only so the engine/ingest are testable without the web stack."""
import os
import sqlite3

DB_PATH = os.environ.get("TARIFF_DB", os.path.join(os.path.dirname(__file__), "..", "data", "tariff.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS readings (
    interval_end TEXT PRIMARY KEY,   -- local Europe/Dublin 'YYYY-MM-DD HH:MM' (end of 30-min interval)
    import_kwh REAL NOT NULL DEFAULT 0,
    export_kwh REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS plans (
    id INTEGER PRIMARY KEY,
    supplier TEXT NOT NULL,
    name TEXT NOT NULL,
    standing_charge_annual REAL NOT NULL DEFAULT 0,  -- € incl. VAT
    export_rate REAL NOT NULL DEFAULT 0,             -- €/kWh microgen credit
    signup_credit REAL NOT NULL DEFAULT 0,           -- € one-off, year-1 only
    notes TEXT NOT NULL DEFAULT '',
    url TEXT NOT NULL DEFAULT '',
    rates_as_of TEXT NOT NULL DEFAULT '',            -- date the rates were checked
    active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS rate_bands (
    id INTEGER PRIMARY KEY,
    plan_id INTEGER NOT NULL REFERENCES plans(id) ON DELETE CASCADE,
    label TEXT NOT NULL,
    rate REAL NOT NULL,               -- €/kWh incl. VAT (0 for free bands)
    start_time TEXT NOT NULL,         -- 'HH:MM' inclusive
    end_time TEXT NOT NULL,           -- 'HH:MM' exclusive; wraps midnight if end <= start
    dow_mask INTEGER NOT NULL DEFAULT 127,  -- bit 0 = Monday ... bit 6 = Sunday
    priority INTEGER NOT NULL DEFAULT 0     -- higher wins on overlap (peak/EV above day/night)
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def connect(path: str | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(path or DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn


def get_setting(conn, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
    conn.commit()
