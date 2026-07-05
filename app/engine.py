"""Costing engine: replay half-hourly readings against a plan's rate bands.

Band assignment uses the interval START (end - 30 min) on the local clock, since a
reading ending 17:00 is consumption for 16:30-17:00. Bands may wrap midnight
(start >= end means e.g. 23:00-08:00). On overlap the highest-priority band wins;
ties broken by lowest id. A plan whose bands leave a gap raises PlanCoverageError
so a typo can't silently undercost a plan.
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta


class PlanCoverageError(Exception):
    pass


@dataclass
class Band:
    label: str
    rate: float
    start_time: str  # 'HH:MM'
    end_time: str    # 'HH:MM' exclusive; wraps if <= start
    dow_mask: int = 127
    priority: int = 0
    id: int = 0

    def matches(self, t: datetime) -> bool:
        if not (self.dow_mask >> t.weekday()) & 1:
            return False
        hm = t.strftime("%H:%M")
        if self.start_time < self.end_time:
            return self.start_time <= hm < self.end_time
        return hm >= self.start_time or hm < self.end_time  # wraps midnight


@dataclass
class Plan:
    supplier: str
    name: str
    standing_charge_annual: float = 0.0
    export_rate: float = 0.0
    signup_credit: float = 0.0
    bands: list[Band] = field(default_factory=list)
    id: int = 0

    def band_for(self, t: datetime) -> Band:
        best = None
        for b in self.bands:
            if b.matches(t) and (best is None or (b.priority, -b.id) > (best.priority, -best.id)):
                best = b
        if best is None:
            raise PlanCoverageError(
                f"{self.supplier} {self.name}: no band covers {t:%A %H:%M} — check band windows/days")
        return best


def cost_plan(plan: Plan, readings: list[tuple[str, float, float]]) -> dict:
    """readings: list of (interval_end 'YYYY-MM-DD HH:MM', import_kwh, export_kwh).

    Returns euro figures for the readings window plus annualised year-1/ongoing totals.
    """
    if not readings:
        raise ValueError("no readings in window")
    per_band: dict[str, dict] = {}
    import_cost = import_kwh = export_kwh = 0.0
    for end_s, imp, exp in readings:
        end = datetime.strptime(end_s, "%Y-%m-%d %H:%M")
        band = plan.band_for(end - timedelta(minutes=30))
        agg = per_band.setdefault(band.label, {"kwh": 0.0, "eur": 0.0, "rate": band.rate})
        agg["kwh"] += imp
        agg["eur"] += imp * band.rate
        import_cost += imp * band.rate
        import_kwh += imp
        export_kwh += exp

    first = datetime.strptime(readings[0][0], "%Y-%m-%d %H:%M")
    last = datetime.strptime(readings[-1][0], "%Y-%m-%d %H:%M")
    days = max((last - first).total_seconds() / 86400, 1.0)
    scale = 365.0 / days

    export_credit = export_kwh * plan.export_rate
    standing = plan.standing_charge_annual * days / 365.0
    window_cost = import_cost - export_credit + standing
    annual_ongoing = (import_cost - export_credit) * scale + plan.standing_charge_annual
    return {
        "window_days": round(days, 1),
        "import_kwh": round(import_kwh, 1),
        "export_kwh": round(export_kwh, 1),
        "import_cost": round(import_cost, 2),
        "export_credit": round(export_credit, 2),
        "standing": round(standing, 2),
        "window_cost": round(window_cost, 2),
        "annual_ongoing": round(annual_ongoing, 2),
        "annual_year1": round(annual_ongoing - plan.signup_credit, 2),
        "per_band": {k: {"kwh": round(v["kwh"], 1), "eur": round(v["eur"], 2), "rate": v["rate"]}
                     for k, v in per_band.items()},
    }


def load_plans(conn, active_only: bool = True) -> list[Plan]:
    plans = []
    q = "SELECT * FROM plans" + (" WHERE active=1" if active_only else "") + " ORDER BY supplier, name"
    for p in conn.execute(q):
        bands = [Band(label=b["label"], rate=b["rate"], start_time=b["start_time"], end_time=b["end_time"],
                      dow_mask=b["dow_mask"], priority=b["priority"], id=b["id"])
                 for b in conn.execute("SELECT * FROM rate_bands WHERE plan_id=?", (p["id"],))]
        plans.append(Plan(supplier=p["supplier"], name=p["name"],
                          standing_charge_annual=p["standing_charge_annual"], export_rate=p["export_rate"],
                          signup_credit=p["signup_credit"], bands=bands, id=p["id"]))
    return plans


def load_readings(conn, days: int = 365) -> list[tuple[str, float, float]]:
    rows = conn.execute(
        "SELECT interval_end, import_kwh, export_kwh FROM readings "
        "WHERE interval_end >= (SELECT DATE(MAX(interval_end), ?) FROM readings) ORDER BY interval_end",
        (f"-{days} days",)).fetchall()
    return [(r["interval_end"], r["import_kwh"], r["export_kwh"]) for r in rows]


def rank_plans(conn, days: int = 365) -> list[dict]:
    readings = load_readings(conn, days)
    results = []
    for plan in load_plans(conn):
        try:
            r = cost_plan(plan, readings)
        except PlanCoverageError as e:
            r = {"error": str(e)}
        r.update({"plan_id": plan.id, "supplier": plan.supplier, "name": plan.name})
        results.append(r)
    results.sort(key=lambda r: r.get("annual_year1", float("inf")))
    return results
