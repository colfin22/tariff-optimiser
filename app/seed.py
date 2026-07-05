"""Seed current Irish supplier plans. Rates researched 05-07-2026 from selectra.ie rate cards
(unit rates there are quoted ex-VAT; we store incl. 9% VAT — the price you actually pay).
Standing charges are urban, already incl. VAT. Export rates as quoted (no VAT added for
non-VAT-registered microgenerators).

Smart tariff windows follow the standard Irish pattern: Day 08:00-23:00, Night 23:00-08:00,
Peak 17:00-19:00. EV boost windows assumed 02:00-06:00 — VERIFY per plan before switching.

Idempotent: does nothing if any plans exist (use --force to wipe and reseed).
"""
import sys

from . import db

AS_OF = "2026-07-05"
VAT = 1.09

ALL_WEEK = 127
DAY = ("Day", "08:00", "23:00", ALL_WEEK, 0)
NIGHT = ("Night", "23:00", "08:00", ALL_WEEK, 0)
PEAK = ("Peak", "17:00", "19:00", ALL_WEEK, 10)
FLAT = ("All hours", "00:00", "00:00", ALL_WEEK, 0)
EV = ("EV boost", "02:00", "06:00", ALL_WEEK, 20)


def v(cent: float) -> float:
    return round(cent * VAT / 100, 4)


# (supplier, plan, standing, export, notes, url, [(band_template, rate_ex_vat_cent)])
PLANS = [
    ("Electric Ireland", "Standard 24hr", 250.77, 0.195, "", "https://selectra.ie/energy/providers/electric-ireland/rates",
     [(FLAT, 31.27)]),
    ("Electric Ireland", "NightSaver", 328.58, 0.195, "Non-smart D/N meter; night window 23:00-08:00 winter (09:00 summer)", "",
     [(DAY, 35.06), (NIGHT, 17.29)]),
    ("Electric Ireland", "Smart Electricity", 250.77, 0.195, "", "",
     [(DAY, 34.99), (NIGHT, 18.39), (PEAK, 37.33)]),
    ("Electric Ireland", "Smart 24", 250.77, 0.195, "", "", [(FLAT, 29.27)]),
    ("Bord Gáis Energy", "Standard 24hr", 244.76, 0.185, "", "https://selectra.ie/energy/providers/bord-gais-energy/rates",
     [(FLAT, 31.20)]),
    ("Bord Gáis Energy", "Smart Electricity", 244.76, 0.185, "", "",
     [(DAY, 33.34), (NIGHT, 24.61), (PEAK, 40.58)]),
    ("Bord Gáis Energy", "Smart All Day", 244.76, 0.185, "", "", [(FLAT, 31.20)]),
    ("SSE Airtricity", "Standard 24hr", 242.07, 0.24, "", "https://selectra.ie/energy/providers/sse-airtricity/rates",
     [(FLAT, 28.30)]),
    ("SSE Airtricity", "Smart Electricity", 242.07, 0.24, "", "",
     [(DAY, 29.95), (NIGHT, 19.25), (PEAK, 33.54)]),
    ("SSE Airtricity", "Smart Everyday", 243.86, 0.24, "", "", [(FLAT, 30.85)]),
    ("Energia", "Standard 24hr", 265.01, 0.20, "", "https://selectra.ie/energy/providers/energia/rates",
     [(FLAT, 27.39)]),
    ("Energia", "Smart 24 Hour", 265.01, 0.20, "", "", [(FLAT, 25.78)]),
    ("Energia", "Smart Day Night", 331.97, 0.20, "Higher standing charge", "",
     [(DAY, 30.03), (NIGHT, 14.40)]),
    ("Energia", "Smart Data", 265.01, 0.20, "", "",
     [(DAY, 30.75), (NIGHT, 16.91), (PEAK, 34.54)]),
    ("Energia", "SST", 265.01, 0.20, "", "",
     [(DAY, 32.85), (NIGHT, 19.26), (PEAK, 36.89)]),
    ("Energia", "EV Smart Drive", 265.01, 0.20, "Flat rate + EV boost window — verify boost hours", "",
     [(FLAT, 36.85), (EV, 8.65)]),
    ("Energia", "EV Smart Drive Plus", 265.01, 0.20, "Verify EV charge-time hours", "",
     [(DAY, 35.72), (NIGHT, 22.01), (PEAK, 46.86), (EV, 10.13)]),
    ("Flogas", "Standard 24hr", 252.65, 0.185, "", "https://selectra.ie/energy/providers/flogas/rates",
     [(FLAT, 29.10)]),
    ("Flogas", "Smart Electricity", 248.12, 0.185, "", "",
     [(DAY, 29.74), (NIGHT, 19.05), (PEAK, 35.19)]),
    ("Flogas", "Smart 24hr", 248.12, 0.185, "", "", [(FLAT, 27.30)]),
    ("Pinergy", "Standard 24hr", 260.06, 0.25, "Pay-as-you-go; best export rate; name matches Selectra for the scraper", "https://selectra.ie/energy/providers/pinergy/rates",
     [(FLAT, 42.77)]),
]


def seed(conn, force: bool = False) -> int:
    if conn.execute("SELECT COUNT(*) n FROM plans").fetchone()["n"]:
        if not force:
            return 0
        conn.execute("DELETE FROM plans")
    for supplier, name, standing, export, notes, url, bands in PLANS:
        cur = conn.execute(
            "INSERT INTO plans(supplier,name,standing_charge_annual,export_rate,signup_credit,notes,url,rates_as_of) "
            "VALUES(?,?,?,?,0,?,?,?)", (supplier, name, standing, export, notes, url, AS_OF))
        for (label, start, end, mask, prio), cent in bands:
            conn.execute("INSERT INTO rate_bands(plan_id,label,rate,start_time,end_time,dow_mask,priority) "
                         "VALUES(?,?,?,?,?,?,?)", (cur.lastrowid, label, v(cent), start, end, mask, prio))
    conn.commit()
    return len(PLANS)


if __name__ == "__main__":
    n = seed(db.connect(), force="--force" in sys.argv)
    print(f"seeded {n} plans" if n else "plans already present — skipped (use --force)")
