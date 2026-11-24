import os
import sqlite3
import tempfile
from datetime import date, timedelta

import pytest

from app import alerts, db


def make_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    return db.connect(path)


def add_flat_plan(conn, supplier, name, rate, standing=278.0, discount_pct=0, discount_months=0, exit_fee=0):
    cur = conn.execute("INSERT INTO plans(supplier,name,standing_charge_annual,discount_pct,discount_months,exit_fee) "
                       "VALUES(?,?,?,?,?,?)", (supplier, name, standing, discount_pct, discount_months, exit_fee))
    conn.execute("INSERT INTO rate_bands(plan_id,label,rate,start_time,end_time) VALUES(?,?,?,?,?)",
                 (cur.lastrowid, "All hours", rate, "00:00", "00:00"))
    conn.commit()
    return cur.lastrowid


def add_year_of_readings(conn, kwh_per_day=13.0):
    d = date(2025, 10, 3)
    rows = [(f"{d + timedelta(days=i):%Y-%m-%d} 12:00", kwh_per_day, 0.0) for i in range(366)]
    conn.executemany("INSERT INTO readings(interval_end,import_kwh,export_kwh) VALUES(?,?,?)", rows)
    conn.commit()


@pytest.fixture
def conn(monkeypatch):
    c = make_db()
    add_year_of_readings(c)
    monkeypatch.setattr(alerts, "_notify", lambda conn, title, msg: None)  # no Home Assistant in tests
    return c


def set_contract(conn, plan_id, start="12-12-2025"):
    db.set_setting(conn, "current_plan_id", str(plan_id))
    db.set_setting(conn, "contract_start", start)
    db.set_setting(conn, "contract_months", "12")


def test_alert_compares_against_current_plans_standard_rate(conn):
    # Current plan is the cheapest while discounted (29.64c), but at its 41.75c standard rate a
    # 33c plan wins. Year-1 vs year-1 would wrongly say "still the cheapest".
    cur = add_flat_plan(conn, "Energia", "Flat", 0.2964, discount_pct=29, discount_months=12)
    add_flat_plan(conn, "Other", "Smart 24", 0.33)
    set_contract(conn, cur)
    r = alerts.check_and_notify(conn, today=date(2026, 11, 20))
    assert r["status"] == "alerted"
    assert "goes back to its standard rate" in r["message"]
    assert "Other Smart 24" in r["message"] and "saves" in r["message"]


def test_alert_says_current_still_cheapest_at_standard_rate(conn):
    cur = add_flat_plan(conn, "Energia", "Flat", 0.2964, discount_pct=29, discount_months=12)
    add_flat_plan(conn, "Other", "Dear", 0.45)
    set_contract(conn, cur)
    r = alerts.check_and_notify(conn, today=date(2026, 11, 20))
    assert "still the cheapest" in r["message"]
    assert "Other Dear" not in r["message"]


def test_alert_unchanged_for_plans_without_discount(conn):
    cur = add_flat_plan(conn, "A", "Flat", 0.30)
    add_flat_plan(conn, "B", "Flat", 0.28)
    set_contract(conn, cur)
    r = alerts.check_and_notify(conn, today=date(2026, 11, 20))
    assert "standard rate" not in r["message"]
    assert "Cheapest for your usage: B Flat" in r["message"]


def test_renewal_choice_never_picks_current_as_best():
    ranking = [
        {"plan_id": 1, "supplier": "A", "name": "x", "annual_year1": 1000, "annual_ongoing": 1500},
        {"plan_id": 2, "supplier": "B", "name": "y", "annual_year1": 1200, "annual_ongoing": 1200},
    ]
    c = alerts.renewal_choice(ranking, "1")
    assert c["best"]["plan_id"] == 2
    assert c["stay_cost"] == 1500
    assert c["saving"] == 300


def test_old_database_gets_discount_columns():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE plans (id INTEGER PRIMARY KEY, supplier TEXT NOT NULL, name TEXT NOT NULL, "
                "standing_charge_annual REAL NOT NULL DEFAULT 0, export_rate REAL NOT NULL DEFAULT 0, "
                "signup_credit REAL NOT NULL DEFAULT 0, notes TEXT NOT NULL DEFAULT '', url TEXT NOT NULL DEFAULT '', "
                "rates_as_of TEXT NOT NULL DEFAULT '', active INTEGER NOT NULL DEFAULT 1)")
    old.execute("INSERT INTO plans(supplier,name) VALUES('A','x')")
    old.commit()
    old.close()
    conn = db.connect(path)
    row = conn.execute("SELECT discount_pct, discount_months, exit_fee FROM plans").fetchone()
    assert (row["discount_pct"], row["discount_months"], row["exit_fee"]) == (0, 0, 0)


# --- exit fee -------------------------------------------------------------------------------------

def _ranking(fee):
    return [
        {"plan_id": 1, "supplier": "A", "name": "x", "annual_year1": 1000, "annual_ongoing": 1100, "exit_fee": fee},
        {"plan_id": 2, "supplier": "B", "name": "y", "annual_year1": 1070, "annual_ongoing": 1070, "exit_fee": 0},
    ]


def test_exit_fee_applies_only_before_contract_end():
    before = alerts.renewal_choice(_ranking(50), "1", before_contract_end=True)
    after = alerts.renewal_choice(_ranking(50), "1", before_contract_end=False)
    assert before["exit_fee_applied"] == 50 and before["saving"] == -20 and before["saving_at_end"] == 30
    assert after["exit_fee_applied"] == 0 and after["saving"] == 30


def test_exit_fee_of_another_plan_is_ignored():
    r = _ranking(0)
    r[1]["exit_fee"] = 99  # fee on the plan you'd switch TO doesn't apply to leaving the current one
    assert alerts.renewal_choice(r, "1", before_contract_end=True)["saving"] == 30


def test_alert_mentions_exit_fee_when_it_cancels_the_saving(conn):
    cur = add_flat_plan(conn, "A", "Flat", 0.300, exit_fee=50)
    add_flat_plan(conn, "B", "Flat", 0.295)  # about €24/yr cheaper on 4,758 kWh
    set_contract(conn, cur)
    r = alerts.check_and_notify(conn, today=date(2026, 11, 20))
    assert "Cheapest for your usage: B Flat" in r["message"]
    assert "€50 exit fee, which cancels the saving" in r["message"]


def test_alert_has_no_exit_fee_on_contract_end_day(conn):
    cur = add_flat_plan(conn, "A", "Flat", 0.300, exit_fee=50)
    add_flat_plan(conn, "B", "Flat", 0.28)
    set_contract(conn, cur)
    r = alerts.check_and_notify(conn, today=date(2026, 12, 12))
    assert r["status"] == "alerted"
    assert "exit fee" not in r["message"]


# --- alert thresholds -------------------------------------------------------------------------------

def test_seven_day_alert_fires_after_the_30_day_one(conn):
    cur = add_flat_plan(conn, "A", "Flat", 0.30)
    add_flat_plan(conn, "B", "Flat", 0.28)
    set_contract(conn, cur)  # contract ends 12-12-2026
    first = alerts.check_and_notify(conn, today=date(2026, 11, 20))  # 22 days left
    assert (first["status"], first["threshold"]) == ("alerted", 30)
    again = alerts.check_and_notify(conn, today=date(2026, 11, 25))  # 17 days left
    assert again["status"] == "already alerted"
    week = alerts.check_and_notify(conn, today=date(2026, 12, 6))  # 6 days left
    assert (week["status"], week["threshold"]) == ("alerted", 7)
    assert alerts.check_and_notify(conn, today=date(2026, 12, 8))["status"] == "already alerted"


def test_first_check_inside_the_last_week_sends_the_7_day_alert(conn):
    cur = add_flat_plan(conn, "A", "Flat", 0.30)
    set_contract(conn, cur)
    r = alerts.check_and_notify(conn, today=date(2026, 12, 9))  # 3 days left, nothing sent yet
    assert (r["status"], r["threshold"]) == ("alerted", 7)
