import json
import os
from datetime import date

from fastapi import FastAPI, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import alerts, db, engine, ingest, scraper

app = FastAPI(title="Irish Tariff Optimiser")
BASE = os.path.dirname(__file__)
app.mount("/static", StaticFiles(directory=os.path.join(BASE, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(BASE, "templates"))

DOW_LABELS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def conn():
    return db.connect()


def dow_names(mask: int) -> str:
    if mask == 127:
        return "every day"
    return ",".join(l for i, l in enumerate(DOW_LABELS) if (mask >> i) & 1)


templates.env.filters["dow"] = dow_names


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/")
def dashboard(request: Request, days: int = 365):
    c = conn()
    try:
        freshness = c.execute("SELECT MAX(interval_end) m, COUNT(*) n FROM readings").fetchone()
        ranking = engine.rank_plans(c, days) if freshness["n"] else []
        current_id = db.get_setting(c, "current_plan_id")
        current = next((r for r in ranking if str(r.get("plan_id")) == current_id), None)
        end = alerts.contract_end(c)
        return templates.TemplateResponse(request, "dashboard.html", {
            "ranking": ranking, "days": days, "current": current, "current_id": current_id,
            "last_reading": freshness["m"], "n_readings": freshness["n"],
            "contract_end": end, "days_left": (end - date.today()).days if end else None,
        })
    finally:
        c.close()


@app.get("/plans")
def plans_page(request: Request):
    c = conn()
    try:
        c.executescript(scraper.SCHEMA)
        plans = c.execute("SELECT * FROM plans ORDER BY active DESC, supplier, name").fetchall()
        bands = {}
        for b in c.execute("SELECT * FROM rate_bands ORDER BY plan_id, priority DESC, start_time"):
            bands.setdefault(b["plan_id"], []).append(b)
        suggestions = c.execute("SELECT * FROM suggestions WHERE status='pending' ORDER BY id").fetchall()
        return templates.TemplateResponse(request, "plans.html",
                                          {"plans": plans, "bands": bands, "suggestions": suggestions,
                                           "current_id": db.get_setting(c, "current_plan_id")})
    finally:
        c.close()


@app.post("/api/scrape")
def api_scrape():
    c = conn()
    try:
        return scraper.scrape_and_notify(c)
    finally:
        c.close()


@app.post("/suggestions/{sid}/apply")
def suggestion_apply(sid: int):
    c = conn()
    try:
        scraper.apply_suggestion(c, sid)
    finally:
        c.close()
    return RedirectResponse("/plans", status_code=303)


@app.post("/suggestions/{sid}/dismiss")
def suggestion_dismiss(sid: int):
    c = conn()
    try:
        c.execute("UPDATE suggestions SET status='dismissed' WHERE id=?", (sid,))
        c.commit()
    finally:
        c.close()
    return RedirectResponse("/plans", status_code=303)


@app.post("/suggestions/apply_all")
def suggestions_apply_all():
    c = conn()
    try:
        for s in c.execute("SELECT id FROM suggestions WHERE status='pending' AND plan_id IS NOT NULL").fetchall():
            scraper.apply_suggestion(c, s["id"])
    finally:
        c.close()
    return RedirectResponse("/plans", status_code=303)


@app.post("/plans/new")
def plan_new(supplier: str = Form(...), name: str = Form(...), standing: float = Form(0),
             export_rate: float = Form(0), signup_credit: float = Form(0),
             rates_as_of: str = Form(""), url: str = Form(""), notes: str = Form("")):
    c = conn()
    try:
        c.execute("INSERT INTO plans(supplier,name,standing_charge_annual,export_rate,signup_credit,rates_as_of,url,notes) "
                  "VALUES(?,?,?,?,?,?,?,?)", (supplier, name, standing, export_rate, signup_credit, rates_as_of, url, notes))
        c.commit()
    finally:
        c.close()
    return RedirectResponse("/plans", status_code=303)


@app.post("/plans/{plan_id}/update")
def plan_update(plan_id: int, supplier: str = Form(...), name: str = Form(...), standing: float = Form(0),
                export_rate: float = Form(0), signup_credit: float = Form(0),
                rates_as_of: str = Form(""), url: str = Form(""), notes: str = Form(""), active: int = Form(1)):
    c = conn()
    try:
        c.execute("UPDATE plans SET supplier=?,name=?,standing_charge_annual=?,export_rate=?,signup_credit=?,"
                  "rates_as_of=?,url=?,notes=?,active=? WHERE id=?",
                  (supplier, name, standing, export_rate, signup_credit, rates_as_of, url, notes, active, plan_id))
        c.commit()
    finally:
        c.close()
    return RedirectResponse("/plans", status_code=303)


@app.post("/plans/{plan_id}/delete")
def plan_delete(plan_id: int):
    c = conn()
    try:
        c.execute("DELETE FROM plans WHERE id=?", (plan_id,))
        c.commit()
    finally:
        c.close()
    return RedirectResponse("/plans", status_code=303)


@app.post("/plans/{plan_id}/duplicate")
def plan_duplicate(plan_id: int):
    c = conn()
    try:
        p = c.execute("SELECT * FROM plans WHERE id=?", (plan_id,)).fetchone()
        cur = c.execute("INSERT INTO plans(supplier,name,standing_charge_annual,export_rate,signup_credit,rates_as_of,url,notes,active) "
                        "VALUES(?,?,?,?,?,?,?,?,?)",
                        (p["supplier"], p["name"] + " (copy)", p["standing_charge_annual"], p["export_rate"],
                         p["signup_credit"], p["rates_as_of"], p["url"], p["notes"], p["active"]))
        for b in c.execute("SELECT * FROM rate_bands WHERE plan_id=?", (plan_id,)).fetchall():
            c.execute("INSERT INTO rate_bands(plan_id,label,rate,start_time,end_time,dow_mask,priority) VALUES(?,?,?,?,?,?,?)",
                      (cur.lastrowid, b["label"], b["rate"], b["start_time"], b["end_time"], b["dow_mask"], b["priority"]))
        c.commit()
    finally:
        c.close()
    return RedirectResponse("/plans", status_code=303)


@app.post("/plans/{plan_id}/bands/new")
def band_new(plan_id: int, label: str = Form(...), rate: float = Form(...), start_time: str = Form(...),
             end_time: str = Form(...), priority: int = Form(0), dow: list[int] = Form([0, 1, 2, 3, 4, 5, 6])):
    mask = sum(1 << d for d in dow)
    c = conn()
    try:
        c.execute("INSERT INTO rate_bands(plan_id,label,rate,start_time,end_time,dow_mask,priority) VALUES(?,?,?,?,?,?,?)",
                  (plan_id, label, rate, start_time, end_time, mask, priority))
        c.commit()
    finally:
        c.close()
    return RedirectResponse("/plans", status_code=303)


@app.post("/bands/{band_id}/delete")
def band_delete(band_id: int):
    c = conn()
    try:
        c.execute("DELETE FROM rate_bands WHERE id=?", (band_id,))
        c.commit()
    finally:
        c.close()
    return RedirectResponse("/plans", status_code=303)


@app.get("/usage")
def usage(request: Request, days: int = 365):
    c = conn()
    try:
        profile = c.execute(
            "SELECT substr(interval_end,12,5) hm, AVG(import_kwh) imp, AVG(export_kwh) exp FROM readings "
            "WHERE interval_end >= (SELECT DATE(MAX(interval_end), ?) FROM readings) "
            "GROUP BY hm ORDER BY hm", (f"-{days} days",)).fetchall()
        monthly = c.execute(
            "SELECT substr(interval_end,1,7) ym, SUM(import_kwh) imp, SUM(export_kwh) exp "
            "FROM readings GROUP BY ym ORDER BY ym").fetchall()
        return templates.TemplateResponse(request, "usage.html", {
            "days": days,
            "profile": json.dumps([{"hm": r["hm"], "imp": round(r["imp"], 3), "exp": round(r["exp"], 3)} for r in profile]),
            "monthly": json.dumps([{"ym": r["ym"], "imp": round(r["imp"], 1), "exp": round(r["exp"], 1)} for r in monthly]),
        })
    finally:
        c.close()


@app.get("/settings")
def settings_page(request: Request):
    c = conn()
    try:
        plans = c.execute("SELECT id,supplier,name FROM plans WHERE active=1 ORDER BY supplier,name").fetchall()
        s = {k: db.get_setting(c, k) for k in
             ("current_plan_id", "contract_start", "contract_months", "ha_url", "ha_token", "notify_service")}
        return templates.TemplateResponse(request, "settings.html", {"plans": plans, "s": s})
    finally:
        c.close()


@app.post("/settings")
def settings_save(current_plan_id: str = Form(""), contract_start: str = Form(""), contract_months: str = Form("12"),
                  ha_url: str = Form(""), ha_token: str = Form(""), notify_service: str = Form("")):
    c = conn()
    try:
        for k, v in (("current_plan_id", current_plan_id), ("contract_start", contract_start),
                     ("contract_months", contract_months), ("ha_url", ha_url), ("notify_service", notify_service)):
            db.set_setting(c, k, v)
        if ha_token:  # blank = keep existing
            db.set_setting(c, "ha_token", ha_token)
    finally:
        c.close()
    return RedirectResponse("/settings", status_code=303)


@app.post("/api/ingest")
def api_ingest(path: str = "/data/esbn_hdf_latest.csv"):
    c = conn()
    try:
        return ingest.ingest_file(c, path)
    finally:
        c.close()


@app.post("/api/alerts/check")
def api_alerts():
    c = conn()
    try:
        return alerts.check_and_notify(c)
    finally:
        c.close()


@app.post("/api/notify/test")
def api_notify_test():
    c = conn()
    try:
        alerts._notify(c, "Tariff Optimiser", "Test notification — settings are working.")
        return {"sent": True}
    finally:
        c.close()
