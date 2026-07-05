import os
import tempfile

from app import db, ingest

SAMPLE = """MPRN,Meter Serial Number,Read Value,Read Type,Read Date and End Time
10000000000,000000000000000001,0.2155,Active Import Interval (kWh),03-07-2026 02:00
10000000000,000000000000000001,0.0000,Active Export Interval (kWh),03-07-2026 02:00
10000000000,000000000000000001,0.5000,Active Import Interval (kWh),03-07-2026 02:30
10000000000,000000000000000001,1.2345,Some Future Read Type,03-07-2026 03:00
"""


def make_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    return db.connect(path), path


def write_sample(content=SAMPLE):
    fd, path = tempfile.mkstemp(suffix=".csv")
    with os.fdopen(fd, "w") as f:
        f.write(content)
    return path


def test_parse_and_upsert():
    conn, dbp = make_db()
    csvp = write_sample()
    try:
        r = ingest.ingest_file(conn, csvp)
        assert r["intervals"] == 2
        assert r["skipped_rows"] == 1
        row = conn.execute("SELECT * FROM readings WHERE interval_end='2026-07-03 02:00'").fetchone()
        assert row["import_kwh"] == 0.2155 and row["export_kwh"] == 0.0
    finally:
        os.unlink(dbp), os.unlink(csvp)


def test_idempotent_and_updates():
    conn, dbp = make_db()
    csvp = write_sample()
    try:
        ingest.ingest_file(conn, csvp)
        n1 = conn.execute("SELECT COUNT(*) n FROM readings").fetchone()["n"]
        # re-run with a corrected value for an existing interval
        csvp2 = write_sample(SAMPLE.replace("0.2155", "0.9999"))
        ingest.ingest_file(conn, csvp2)
        n2 = conn.execute("SELECT COUNT(*) n FROM readings").fetchone()["n"]
        assert n1 == n2 == 2
        row = conn.execute("SELECT import_kwh FROM readings WHERE interval_end='2026-07-03 02:00'").fetchone()
        assert row["import_kwh"] == 0.9999
        os.unlink(csvp2)
    finally:
        os.unlink(dbp), os.unlink(csvp)
