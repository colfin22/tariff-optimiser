"""Scrape-and-suggest: pull current rates from selectra.ie rate cards and queue
suggestions where they differ from stored plans. Never auto-applies — a human
reviews on the Plans page. Selectra quotes unit rates ex-VAT (we store incl. 9%)
and urban standing charges incl. VAT; band time WINDOWS are not published there,
so only rates/standing/export are compared.
"""
import html
import re
import sqlite3
import urllib.request
from datetime import date

from . import db

PAGES = {
    "Electric Ireland": "https://selectra.ie/energy/providers/electric-ireland/rates",
    "Bord Gáis Energy": "https://selectra.ie/energy/providers/bord-gais-energy/rates",
    "SSE Airtricity": "https://selectra.ie/energy/providers/sse-airtricity/rates",
    "Energia": "https://selectra.ie/energy/providers/energia/rates",
    "Flogas": "https://selectra.ie/energy/providers/flogas/rates",
    "Pinergy": "https://selectra.ie/energy/providers/pinergy/rates",
}
UA = "Mozilla/5.0 (X11; Linux x86_64) tariff-optimiser/1.0 (personal rate check, monthly)"
VAT = 1.09

# Selectra row label -> our band label. Longest-first so 'Night Boost' wins over 'Night'.
LABEL_MAP = [
    ("EV Charge Time", "EV boost"), ("Night Boost", "EV boost"), ("24-Hour", "All hours"),
    ("Night", "Night"), ("Peak", "Peak"), ("Day", "Day"),
]
ROW_RE = re.compile(r"(EV Charge Time|Night Boost|24-Hour|Night|Peak|Day)\s+(\d+\.\d+)\s*c/kWh")
STANDING_RE = re.compile(r"Standing charge\s*€\s*(\d+\.\d+)\s*/yr")
EXPORT_RE = re.compile(r"(\d+\.\d+)\s*c/kWh")

SCHEMA = """
CREATE TABLE IF NOT EXISTS suggestions (
    id INTEGER PRIMARY KEY,
    plan_id INTEGER,                  -- NULL for informational (new/unmatched plan)
    field TEXT NOT NULL,              -- 'band:<label>' | 'standing' | 'export' | 'info'
    current REAL,
    suggested REAL,
    detail TEXT NOT NULL DEFAULT '',
    created TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'  -- pending | applied | dismissed
);
"""


def _text(chunk: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html.unescape(chunk)))


def parse_page(html_src: str) -> dict:
    """Return {'plans': {name: {'bands': {label: rate_incl_vat}, 'standing': eur}}, 'export': eur_per_kwh}."""
    plans = {}
    blocks = re.split(r'id="offer-', html_src)[1:]
    for block in blocks:
        m = re.match(r'[^"]*"[^>]*>([^<]+)<', block)
        if not m:
            continue
        name = m.group(1).replace(",", "").strip()
        if "gas" in name.lower():
            continue
        txt = _text(block[:6000])
        bands = {}
        for label, val in ROW_RE.findall(txt):
            ours = next(o for s, o in LABEL_MAP if s == label)
            bands.setdefault(ours, round(float(val) * VAT / 100, 4))  # first hit = urban
        st = STANDING_RE.search(txt)
        if bands:
            plans[name] = {"bands": bands, "standing": float(st.group(1)) if st else None}
    export = None
    mi = html_src.find("Microgeneration")
    if mi != -1:
        me = EXPORT_RE.search(_text(html_src[mi:mi + 4000]))
        if me:
            export = round(float(me.group(1)) / 100, 4)  # export quoted rate, no VAT added
    return {"plans": plans, "export": export}


def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    return urllib.request.urlopen(req, timeout=30).read().decode("utf-8", "replace")


def _suggest(conn, plan_id, field, current, suggested, detail):
    dup = conn.execute(
        "SELECT 1 FROM suggestions WHERE status='pending' AND plan_id IS ? AND field=? AND suggested IS ? AND detail=?",
        (plan_id, field, suggested, detail)).fetchone()
    if not dup:
        conn.execute("INSERT INTO suggestions(plan_id,field,current,suggested,detail,created) VALUES(?,?,?,?,?,?)",
                     (plan_id, field, current, suggested, detail, date.today().isoformat()))
        return True
    return False


def run_scrape(conn) -> dict:
    conn.executescript(SCHEMA)
    new, errors, unmatched = 0, [], []
    for supplier, url in PAGES.items():
        try:
            data = parse_page(fetch(url))
        except Exception as e:  # noqa: BLE001 - a failed page must alert, not crash the run
            errors.append(f"{supplier}: {e}")
            continue
        if not data["plans"]:
            errors.append(f"{supplier}: parsed 0 plans — page layout may have changed")
            continue
        rows = conn.execute("SELECT * FROM plans WHERE active=1 AND supplier=?", (supplier,)).fetchall()
        by_name = {r["name"]: r for r in rows}
        for pname, scraped in data["plans"].items():
            plan = by_name.get(pname)
            if plan is None:
                unmatched.append(f"{supplier} {pname}")
                continue
            for label, rate in scraped["bands"].items():
                b = conn.execute("SELECT * FROM rate_bands WHERE plan_id=? AND label=?", (plan["id"], label)).fetchone()
                if b and abs(b["rate"] - rate) > 0.00005:
                    new += _suggest(conn, plan["id"], f"band:{label}", b["rate"], rate,
                                    f"{supplier} {pname} {label}")
            if scraped["standing"] is not None and abs(plan["standing_charge_annual"] - scraped["standing"]) > 0.005:
                new += _suggest(conn, plan["id"], "standing", plan["standing_charge_annual"], scraped["standing"],
                                f"{supplier} {pname} standing charge")
            if data["export"] is not None and abs(plan["export_rate"] - data["export"]) > 0.00005:
                new += _suggest(conn, plan["id"], "export", plan["export_rate"], data["export"],
                                f"{supplier} {pname} export rate")
    for u in unmatched:
        new += _suggest(conn, None, "info", None, None, f"Plan on Selectra but not tracked here: {u}")
    conn.commit()
    return {"new_suggestions": new, "errors": errors, "unmatched": unmatched}


def apply_suggestion(conn, sid: int) -> bool:
    s = conn.execute("SELECT * FROM suggestions WHERE id=? AND status='pending'", (sid,)).fetchone()
    if not s or s["plan_id"] is None:
        return False
    if s["field"].startswith("band:"):
        conn.execute("UPDATE rate_bands SET rate=? WHERE plan_id=? AND label=?",
                     (s["suggested"], s["plan_id"], s["field"][5:]))
    elif s["field"] == "standing":
        conn.execute("UPDATE plans SET standing_charge_annual=? WHERE id=?", (s["suggested"], s["plan_id"]))
    elif s["field"] == "export":
        conn.execute("UPDATE plans SET export_rate=? WHERE id=?", (s["suggested"], s["plan_id"]))
    conn.execute("UPDATE plans SET rates_as_of=? WHERE id=?", (date.today().isoformat(), s["plan_id"]))
    conn.execute("UPDATE suggestions SET status='applied' WHERE id=?", (sid,))
    conn.commit()
    return True


def scrape_and_notify(conn) -> dict:
    from . import alerts
    r = run_scrape(conn)
    if r["new_suggestions"] or r["errors"]:
        parts = []
        if r["new_suggestions"]:
            parts.append(f"{r['new_suggestions']} rate change(s) found — review on the Plans page.")
        if r["errors"]:
            parts.append("Scrape problems: " + "; ".join(r["errors"]))
        try:
            alerts._notify(conn, "Tariff rates", " ".join(parts))
        except Exception:  # noqa: BLE001 - notification failure shouldn't fail the scrape
            pass
    return r
