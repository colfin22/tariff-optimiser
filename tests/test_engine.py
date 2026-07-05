import pytest

from app.engine import Band, Plan, PlanCoverageError, cost_plan

ALL = 127
WEEKDAYS = 0b0011111
WEEKEND = 0b1100000


def day_of(readings_day, date="2026-01-14"):  # a Wednesday
    """Build (interval_end, import, export) rows: one kWh figure per half-hour dict {HH:MM_end: kwh}."""
    return [(f"{date} {hm}", kwh, 0.0) for hm, kwh in readings_day]


def full_year_flat(kwh_per_half_hour=0.5):
    # two half-hours per day for 365 days keeps tests fast while spanning a full year
    rows = []
    from datetime import date, timedelta
    d = date(2025, 7, 1)
    for i in range(365):
        rows.append((f"{d + timedelta(days=i):%Y-%m-%d} 10:00", kwh_per_half_hour, 0.2))
        rows.append((f"{d + timedelta(days=i):%Y-%m-%d} 22:00", kwh_per_half_hour, 0.0))
    return rows


def test_flat_plan_simple_day():
    plan = Plan("T", "Flat", standing_charge_annual=365.0,
                bands=[Band("All", 0.30, "00:00", "00:00")])  # wraps = 24h
    rows = day_of([("10:00", 1.0), ("10:30", 2.0)])
    r = cost_plan(plan, rows)
    assert r["import_cost"] == pytest.approx(0.90)
    assert r["per_band"]["All"]["kwh"] == 3.0


def test_day_night_boundary_uses_interval_start():
    # Night 23:00-08:00. A reading ENDING 23:00 is 22:30-23:00 consumption = Day.
    # A reading ending 23:30 starts 23:00 = Night. Ending 08:00 starts 07:30 = Night.
    plan = Plan("T", "DayNight", bands=[
        Band("Day", 0.40, "08:00", "23:00"),
        Band("Night", 0.20, "23:00", "08:00"),
    ])
    r = cost_plan(plan, day_of([("23:00", 1.0), ("23:30", 1.0), ("08:00", 1.0), ("08:30", 1.0)]))
    assert r["per_band"]["Day"]["kwh"] == 2.0   # 22:30 start + 08:00 start
    assert r["per_band"]["Night"]["kwh"] == 2.0  # 23:00 start + 07:30 start


def test_peak_priority_over_day():
    plan = Plan("T", "Smart", bands=[
        Band("Day", 0.35, "08:00", "23:00", priority=0),
        Band("Peak", 0.45, "17:00", "19:00", priority=10),
        Band("Night", 0.18, "23:00", "08:00"),
    ])
    r = cost_plan(plan, day_of([("17:30", 1.0), ("19:00", 1.0), ("19:30", 1.0)]))
    # 17:00 start & 18:30 start = Peak; 19:00 start = Day
    assert r["per_band"]["Peak"]["kwh"] == 2.0
    assert r["per_band"]["Day"]["kwh"] == 1.0


def test_ev_window_inside_night():
    plan = Plan("T", "EV", bands=[
        Band("Day", 0.38, "08:00", "23:00"),
        Band("Night", 0.20, "23:00", "08:00"),
        Band("EV", 0.08, "02:00", "06:00", priority=5),
    ])
    r = cost_plan(plan, day_of([("02:30", 7.0), ("06:30", 1.0)]))
    assert r["per_band"]["EV"]["kwh"] == 7.0
    assert r["per_band"]["Night"]["kwh"] == 1.0


def test_free_sunday():
    plan = Plan("T", "FreeSunday", bands=[
        Band("All", 0.40, "00:00", "00:00", dow_mask=ALL, priority=0),
        Band("FreeSun", 0.0, "09:00", "17:00", dow_mask=0b1000000, priority=10),
    ])
    sun = day_of([("10:00", 2.0)], date="2026-01-18")   # Sunday
    mon = day_of([("10:00", 2.0)], date="2026-01-19")   # Monday
    r = cost_plan(plan, sun + mon)
    assert r["per_band"]["FreeSun"]["eur"] == 0.0
    assert r["per_band"]["All"]["eur"] == pytest.approx(0.80)


def test_export_credit_and_annualisation():
    plan = Plan("T", "Export", standing_charge_annual=300.0, export_rate=0.20, signup_credit=100.0,
                bands=[Band("All", 0.30, "00:00", "00:00")])
    r = cost_plan(plan, full_year_flat())
    assert r["export_kwh"] == pytest.approx(0.2 * 365)
    assert r["export_credit"] == pytest.approx(0.2 * 365 * 0.20)
    # window is ~364 days -> annual figures ≈ window figures + full standing charge
    assert r["annual_ongoing"] == pytest.approx(0.3 * 365 * 0.5 * 2 * (365 / 364.5) - 14.6 + 300.0, rel=0.01)
    assert r["annual_year1"] == pytest.approx(r["annual_ongoing"] - 100.0)


def test_dst_days_do_not_crash():
    plan = Plan("T", "DayNight", bands=[
        Band("Day", 0.40, "08:00", "23:00"),
        Band("Night", 0.20, "23:00", "08:00"),
    ])
    # spring-forward (2026-03-29) and fall-back (2026-10-25) local times
    rows = day_of([("00:30", 1.0), ("03:30", 1.0)], date="2026-03-29") + \
           day_of([("01:30", 1.0), ("02:30", 1.0)], date="2026-10-25")
    r = cost_plan(plan, rows)
    assert r["import_kwh"] == 4.0


def test_gap_raises():
    plan = Plan("T", "Gappy", bands=[Band("Day", 0.4, "08:00", "23:00")])
    with pytest.raises(PlanCoverageError):
        cost_plan(plan, day_of([("03:00", 1.0)]))
