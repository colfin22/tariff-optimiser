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
UA = "Mozilla/5.0 (X11; Linux x86_64) tariff-optimiser/1.0 (personal rate check, weekly)"
VAT = 1.09

# Selectra row label -> our band label. Longest-first so 'Night Boost' wins over 'Night'.
LABEL_MAP = [
    ("EV Charge Time", "EV boost"), ("Night Boost", "EV boost"), ("24-Hour", "All hours"),
    ("Night", "Night"), ("Peak", "Peak"), ("Day", "Day"),
]
ROW_RE = re.compile(r"(EV Charge Time|Night Boost|24-Hour|Night|Peak|Day)\s+(\d+\.\d+)\s*c/kWh")
STANDING_RE = re.compile(r"Standing charge\s*€\s*(\d+\.\d+)\s*/yr")
# The export rate lives in the "<provider> microgeneration export rate comparison table …
# Export rate … Payment method … NN.NN c/kWh" section. Anchor on that phrase, NOT a bare
# "Microgeneration": Selectra now also renders "Microgeneration rates" as a nav-menu link with
# no figure after it, and find("Microgeneration") landing there returned export=None silently (#6).
EXPORT_TABLE_RE = re.compile(r"export rate comparison table.*?(\d+\.\d+)\s*c/kWh", re.I)

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
    # Selectra redesigned the offer cards 14-09-2026 (#25): the old `id="offer-*"` block markers are
    # gone site-wide. Each card now opens with `<p><strong>PlanName</strong></p>` instead — verified
    # present and countable across all 6 tracked supplier pages. Row-level regexes below are
    # unaffected since they run on stripped text, not raw markup.
    blocks = re.split(r'<p><strong>', html_src)[1:]
    for block in blocks:
        m = re.match(r'([^<]+)</strong>', block)
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
        # Selectra lists some plans with no rates published yet (SSE 'Activ8': 0.00 c/kWh in every
        # column). Treating those as real plans queued a permanent untracked-plan suggestion (#19).
        # Only an ALL-zero block is dropped — one zero band is legitimate on a free-hours plan.
        if bands and any(r > 0 for r in bands.values()):
            plans[name] = {"bands": bands, "standing": float(st.group(1)) if st else None}
    export = None
    me = EXPORT_TABLE_RE.search(_text(html_src))
    if me:
        export = round(float(me.group(1)) / 100, 4)  # export quoted ex-VAT (payments are); no VAT added
    return {"plans": plans, "export": export}


def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    return urllib.request.urlopen(req, timeout=30).read().decode("utf-8", "replace")


def _suggest(conn, plan_id, field, current, suggested, detail):
    # pending = don't re-queue; dismissed = the user said no to THIS value, stay quiet unless it changes again
    dup = conn.execute(
        "SELECT 1 FROM suggestions WHERE status IN ('pending','dismissed') AND plan_id IS ? AND field=? AND suggested IS ? AND detail=?",
        (plan_id, field, suggested, detail)).fetchone()
    if not dup:
        conn.execute("INSERT INTO suggestions(plan_id,field,current,suggested,detail,created) VALUES(?,?,?,?,?,?)",
                     (plan_id, field, current, suggested, detail, date.today().isoformat()))
        return True
    return False


def run_scrape(conn) -> dict:
    conn.executescript(SCHEMA)
    # Rate changes and notices are counted apart: a notice has no Apply button by design (#20).
    new, notices, errors, unmatched, withdrawn = 0, 0, [], [], []
    full_page_failures = 0  # #25 - every page failing at once means the scraper itself is broken,
                            # not that every supplier changed on the same day; flagged separately so
                            # scrape_and_notify can raise it above routine per-page noise.
    for supplier, url in PAGES.items():
        try:
            data = parse_page(fetch(url))
        except Exception as e:  # noqa: BLE001 - a failed page must alert, not crash the run
            errors.append(f"{supplier}: {e}")
            full_page_failures += 1
            continue
        if not data["plans"]:
            errors.append(f"{supplier}: parsed 0 plans — page layout may have changed")
            full_page_failures += 1
            continue
        if data["export"] is None:  # never let a missing export rate pass silently (#6)
            errors.append(f"{supplier}: export rate not found — page layout may have changed")
        rows = conn.execute("SELECT * FROM plans WHERE active=1 AND supplier=?", (supplier,)).fetchall()
        by_name = {r["name"]: r for r in rows}
        for pname, scraped in data["plans"].items():
            plan = by_name.get(pname)
            if plan is None:
                if "dual" in pname.lower():
                    # Dual-fuel plans aren't costable (Selectra doesn't publish band time
                    # windows) but Colm wants them visible in the ranked table with a badge
                    # rather than buried as a dismissible notice, since he doesn't have dual
                    # fuel and wants to see at a glance which plans to skip.
                    exists = conn.execute(
                        "SELECT 1 FROM plans WHERE supplier=? AND name=?", (supplier, pname)).fetchone()
                    if not exists:
                        conn.execute(
                            "INSERT INTO plans(supplier,name,standing_charge_annual,export_rate,"
                            "notes,rates_as_of,active,fuel_type) VALUES(?,?,?,?,?,?,1,'dual')",
                            (supplier, pname, scraped["standing"] or 0, data["export"] or 0,
                             "Auto-added from Selectra; no published band time windows, so cost "
                             "can't be estimated.", date.today().isoformat()))
                        new += 1
                else:
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
        # #21 - and the other way: a tracked plan that has vanished from the page. Without this the
        # comparison ran one way only, so a withdrawn plan kept its last-known rates and kept
        # competing in the ranking forever. Reached only on a page that parsed (the error paths
        # above continue first), so a Selectra redesign can't mass-flag a supplier's catalogue.
        for n, plan in by_name.items():
            if n not in data["plans"] and plan["active"]:
                withdrawn.append((supplier, n, plan["id"]))
    for u in unmatched:
        notices += _suggest(conn, None, "info", None, None, f"Plan on Selectra but not tracked here: {u}")
    for supplier, n, plan_id in withdrawn:
        # #24 - the plain info notice (#21) told a human but never offered anything to act on, so a
        # withdrawn plan stayed active and kept competing in the ranking forever. Queue an actual
        # actionable suggestion instead — same apply/dismiss/dedupe flow as a rate change, still
        # never auto-applied.
        new += _suggest(conn, plan_id, "deactivate", 1, 0,
                        f"{supplier} {n} no longer listed on Selectra — deactivate?")
    conn.commit()
    return {"new_suggestions": new, "new_notices": notices, "errors": errors,
            "unmatched": unmatched, "withdrawn": withdrawn,
            "scraper_broken": full_page_failures == len(PAGES)}


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
    elif s["field"] == "deactivate":
        # No rates_as_of bump below — the plan's rates aren't what changed.
        conn.execute("UPDATE plans SET active=0 WHERE id=?", (s["plan_id"],))
    if s["field"] != "deactivate":
        conn.execute("UPDATE plans SET rates_as_of=? WHERE id=?", (date.today().isoformat(), s["plan_id"]))
    conn.execute("UPDATE suggestions SET status='applied' WHERE id=?", (sid,))
    conn.commit()
    return True


def scrape_and_notify(conn) -> dict:
    from . import alerts
    r = run_scrape(conn)
    parts = []
    if r["new_suggestions"]:
        parts.append(f"{r['new_suggestions']} rate change(s) found — review on the Plans page.")
    if r["new_notices"]:  # #20 - a notice is information, not something to apply
        parts.append(f"{r['new_notices']} notice(s) from the scrape.")
    if r["errors"]:
        parts.append("Scrape problems: " + "; ".join(r["errors"]))
    if not parts:  # #18 - always report, so a dead scrape can't look like a quiet week
        parts.append("Weekly rate scrape ran, no changes found.")
    if r.get("scraper_broken"):
        # #25 - every supplier page failed at once: almost certainly the scraper itself is broken
        # (a site-wide redesign, as happened 14-09-2026), not six suppliers changing on the same
        # day. Say so up front so this can't blend into a routine single-page hiccup.
        parts.insert(0, "CRITICAL: every supplier page failed to parse — the scraper is likely broken (site redesign?), not just one page.")
    try:
        alerts._notify(conn, "Tariff rates", " ".join(parts))
    except Exception as e:  # noqa: BLE001 - notification failure shouldn't fail the scrape
        print(f"scrape_and_notify: push notification failed: {e}")
    return r
