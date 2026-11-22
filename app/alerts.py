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
    msg = (f"Electricity contract ends {end:%d-%m-%Y} ({days_left} days)."
           + renewal_message(ranking, current_id, before_contract_end=today < end))

    _notify(conn, "Electricity contract", msg)
    db.set_setting(conn, marker_key, "1")
    return {"status": "alerted", "threshold": threshold, "days_left": days_left, "message": msg}


def renewal_choice(ranking: list[dict], current_id: str, before_contract_end: bool = False) -> dict:
    """What renewing actually compares: staying on the current plan once its contract ends (its
    annual_ongoing — the standard rate, no signup credit) against the cheapest other plan's
    annual_year1. Comparing year-1 against year-1 would measure the alternatives against a
    discount that is about to end.

    Before the contract end date, switching also costs the current plan's exit fee: `saving` takes
    it off (switching now), `saving_at_end` doesn't (waiting until the end date, when there is no
    fee). From the end date on, both are the same."""
    current = next((r for r in ranking if str(r.get("plan_id")) == current_id), None) if current_id else None
    alternatives = [r for r in ranking if "error" not in r and r is not current]
    best = min(alternatives, key=lambda r: r["annual_year1"], default=None)
    stay = current["annual_ongoing"] if current else None
    fee = current.get("exit_fee", 0) if current and before_contract_end else 0
    at_end = round(stay - best["annual_year1"], 2) if current and best else None
    return {"current": current, "best": best, "stay_cost": stay, "exit_fee_applied": fee,
            "saving_at_end": at_end, "saving": round(at_end - fee, 2) if at_end is not None else None}


def renewal_message(ranking: list[dict], current_id: str, before_contract_end: bool = False) -> str:
    c = renewal_choice(ranking, current_id, before_contract_end)
    current, best, stay, fee = c["current"], c["best"], c["stay_cost"], c["exit_fee_applied"]
    if current is None:  # no current plan set: just name the cheapest
        return f" Cheapest for your usage: {best['supplier']} {best['name']} — €{best['annual_year1']:.0f}/yr." if best else ""
    rolls_back = current.get("discount_pct", 0) > 0 and current.get("discount_months", 0) > 0
    stay_txt = (f" Staying on {current['supplier']} {current['name']} goes back to its standard rate: about €{stay:.0f}/yr."
                if rolls_back else "")
    if best and c["saving_at_end"] > 0:
        msg = (f" Cheapest for your usage: {best['supplier']} {best['name']} — €{best['annual_year1']:.0f}/yr "
               f"(saves €{c['saving_at_end']:.0f}")
        if fee:
            msg += (f" if you switch when the contract ends; switching before then costs the €{fee:.0f} exit fee"
                    + (f", saving €{c['saving']:.0f}" if c["saving"] > 0 else ", which cancels the saving"))
        return stay_txt + msg + ")."
    if rolls_back:
        return stay_txt + " That is still the cheapest option for your usage."
    return f" Your current plan is still the cheapest (€{stay:.0f}/yr)."


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
