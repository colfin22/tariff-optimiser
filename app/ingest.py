"""Parse the ESB Networks HDF export into the readings table.

HDF format (one row per 30-min interval per direction):
    MPRN,Meter Serial Number,Read Value,Read Type,Read Date and End Time
    10000000000,0000...,0.2155,Active Import Interval (kWh),03-07-2026 02:00

Timestamps are local Europe/Dublin clock time (interval END). Stored verbatim as
'YYYY-MM-DD HH:MM' so tariff windows (defined on local clock) need no TZ handling.
"""
import csv
import sys

from . import db

IMPORT_TYPE = "Active Import Interval (kWh)"
EXPORT_TYPE = "Active Export Interval (kWh)"


def _iso(dmy_hm: str) -> str:
    # '03-07-2026 02:00' -> '2026-07-03 02:00'
    date, time = dmy_hm.strip().split(" ")
    d, m, y = date.split("-")
    return f"{y}-{m}-{d} {time}"


def ingest_file(conn, csv_path: str) -> dict:
    intervals: dict[str, list[float]] = {}  # iso_end -> [import, export]
    skipped = 0
    with open(csv_path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rtype = row["Read Type"].strip()
            if rtype not in (IMPORT_TYPE, EXPORT_TYPE):
                skipped += 1
                continue
            key = _iso(row["Read Date and End Time"])
            slot = intervals.setdefault(key, [0.0, 0.0])
            slot[0 if rtype == IMPORT_TYPE else 1] = float(row["Read Value"])
    conn.executemany(
        "INSERT INTO readings(interval_end, import_kwh, export_kwh) VALUES(?,?,?) "
        "ON CONFLICT(interval_end) DO UPDATE SET import_kwh=excluded.import_kwh, export_kwh=excluded.export_kwh",
        [(k, v[0], v[1]) for k, v in intervals.items()],
    )
    conn.commit()
    span = conn.execute("SELECT MIN(interval_end) a, MAX(interval_end) b, COUNT(*) n FROM readings").fetchone()
    return {"intervals": len(intervals), "skipped_rows": skipped,
            "db_rows": span["n"], "first": span["a"], "last": span["b"]}


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else "/data/esbn_hdf_latest.csv"
    print(ingest_file(db.connect(), path))
