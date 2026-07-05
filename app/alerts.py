"""Contract-expiry alert: nudge the phone when the current plan's discount year is ending."""
import json
import urllib.request
from datetime import date, timedelta

from . import db, engine


def contract_end(conn) -> date | None:
    start = db.get_setting(conn, "contract_start")
    months = int(db.get_setting(conn, "contract_months", "12"))
    if not start:
        return None
    d, m, y = (int(x) for x in start.split("-"))  # DD-MM-YYYY
    m += months
    y, m = y + (m - 1) // 12, (m - 1) % 12 + 1
    try:
        return date(y, m, d)
    except ValueError:  # e.g. 31st into a shorter month
        return date(y, m + 1, 1) - timedelta(days=1)


def check_and_notify(conn, today: date | None = None) -> dict:
    today = today or date.today()
    end = contract_end(conn)
    if end is None:
        return {"status": "no contract date set"}
    days_left = (end - today).days
    threshold = next((t for t in (30, 7) if days_left <= t), None)
    if threshold is None or days_left < 0:
        return {"status": "ok", "days_left": days_left}
    marker_key = f"alerted_{end.isoformat()}_{threshold}"
    if db.get_setting(conn, marker_key):
        return {"status": "already alerted", "days_left": days_left}

    ranking = [r for r in engine.rank_plans(conn) if "error" not in r]
    current_id = db.get_setting(conn, "current_plan_id")
    best = ranking[0] if ranking else None
    current = next((r for r in ranking if str(r["plan_id"]) == current_id), None)
    msg = f"Electricity contract ends {end:%d-%m-%Y} ({days_left} days)."
    if best and current and best["plan_id"] != current["plan_id"]:
        saving = current["annual_year1"] - best["annual_year1"]
        msg += f" Cheapest for your usage: {best['supplier']} {best['name']} — €{best['annual_year1']:.0f}/yr (saves €{saving:.0f})."
    elif best:
        msg += f" Your current plan is still the cheapest (€{best['annual_year1']:.0f}/yr)."

    _notify(conn, "Electricity contract", msg)
    db.set_setting(conn, marker_key, "1")
    return {"status": "alerted", "threshold": threshold, "days_left": days_left, "message": msg}


def _notify(conn, title: str, message: str) -> None:
    ha_url = db.get_setting(conn, "ha_url", "http://homeassistant.local:8123")
    token = db.get_setting(conn, "ha_token")
    service = db.get_setting(conn, "notify_service", "mobile_app_np3")
    if not token:
        raise RuntimeError("ha_token not configured in settings")
    req = urllib.request.Request(
        f"{ha_url}/api/services/notify/{service}",
        data=json.dumps({"title": title, "message": message}).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    urllib.request.urlopen(req, timeout=15)


if __name__ == "__main__":
    print(check_and_notify(db.connect()))
