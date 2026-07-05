import os
import tempfile

from app import db, scraper

# Condensed from a real selectra.ie rate card (structure identical, values arbitrary)
FIXTURE = """
<h3 id="offer-acme-standard-24hr" class="x">Standard, 24hr</h3>
<table><tr><th>Time of use</th><th>Urban</th><th>Rural</th></tr>
<tr><td>24-Hour</td><td>30.00 c/kWh</td><td>31.00 c/kWh</td></tr>
<tr><td>Standing charge</td><td>&euro;250.00/yr</td><td>&euro;280.00/yr</td></tr></table>
<h3 id="offer-acme-smart-electricity" class="x">Smart Electricity</h3>
<table>
<tr><td>Day</td><td>33.34 c/kWh</td><td>34.00 c/kWh</td></tr>
<tr><td>Night</td><td>24.61 c/kWh</td><td>25.00 c/kWh</td></tr>
<tr><td>Peak</td><td>40.58 c/kWh</td><td>41.00 c/kWh</td></tr>
<tr><td>Standing charge</td><td>&euro;244.76/yr</td><td>&euro;281.89/yr</td></tr></table>
<h3 id="offer-acme-ev-drive" class="x">EV Smart Drive</h3>
<table>
<tr><td>Day</td><td>36.85 c/kWh</td><td>37.00 c/kWh</td></tr>
<tr><td>Night Boost</td><td>8.65 c/kWh</td><td>9.00 c/kWh</td></tr></table>
<h3 id="offer-acme-standard-gas" class="x">Standard Gas</h3>
<table><tr><td>24-Hour</td><td>9.10 c/kWh</td><td>9.10 c/kWh</td></tr></table>
<h2>Microgeneration rates</h2>
<table><tr><td>Rate</td><td>18.50 c/kWh</td><td>Quarterly credit</td></tr></table>
"""


def test_parse_page():
    r = scraper.parse_page(FIXTURE)
    assert set(r["plans"]) == {"Standard 24hr", "Smart Electricity", "EV Smart Drive"}  # gas skipped, comma stripped
    flat = r["plans"]["Standard 24hr"]
    assert flat["bands"] == {"All hours": round(30.00 * 1.09 / 100, 4)}
    assert flat["standing"] == 250.00  # urban column
    smart = r["plans"]["Smart Electricity"]
    assert smart["bands"]["Peak"] == round(40.58 * 1.09 / 100, 4)
    ev = r["plans"]["EV Smart Drive"]
    assert ev["bands"]["EV boost"] == round(8.65 * 1.09 / 100, 4)  # Night Boost mapped, not Night
    assert "Night" not in ev["bands"]
    assert r["export"] == 0.185


def test_suggest_and_apply(monkeypatch):
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = db.connect(path)
    try:
        conn.executescript(scraper.SCHEMA)
        cur = conn.execute("INSERT INTO plans(supplier,name,standing_charge_annual,export_rate) VALUES('Acme','Smart Electricity',240.0,0.20)")
        pid = cur.lastrowid
        conn.execute("INSERT INTO rate_bands(plan_id,label,rate,start_time,end_time) VALUES(?,?,?,?,?)",
                     (pid, "Day", 0.3500, "08:00", "23:00"))
        conn.commit()
        monkeypatch.setattr(scraper, "PAGES", {"Acme": "http://fixture"})
        monkeypatch.setattr(scraper, "fetch", lambda url: FIXTURE)
        r = scraper.run_scrape(conn)
        assert not r["errors"]
        pending = conn.execute("SELECT * FROM suggestions WHERE status='pending' AND plan_id=?", (pid,)).fetchall()
        fields = {s["field"] for s in pending}
        assert fields == {"band:Day", "standing", "export"}
        # re-run: no duplicates
        scraper.run_scrape(conn)
        assert len(conn.execute("SELECT * FROM suggestions WHERE status='pending' AND plan_id=?", (pid,)).fetchall()) == len(pending)
        # apply the band suggestion
        sid = next(s["id"] for s in pending if s["field"] == "band:Day")
        assert scraper.apply_suggestion(conn, sid)
        b = conn.execute("SELECT rate FROM rate_bands WHERE plan_id=? AND label='Day'", (pid,)).fetchone()
        assert b["rate"] == round(33.34 * 1.09 / 100, 4)
        assert conn.execute("SELECT rates_as_of FROM plans WHERE id=?", (pid,)).fetchone()["rates_as_of"] != ""
        # unmatched plans become info suggestions
        infos = conn.execute("SELECT detail FROM suggestions WHERE field='info'").fetchall()
        assert any("Standard 24hr" in i["detail"] for i in infos)
    finally:
        os.unlink(path)
