"""Unit tests for the AIS recorder + coverage test (ticket #6).

    python -m unittest discover -s tests -v

Uses the synthetic fixture tests/fixtures/ais_sample.jsonl and an in-memory
or temporary SQLite store. No network, no API key.
"""
import io
import json
import logging
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "scripts"))
import ais_lib as L  # noqa: E402
import ais_recorder as R  # noqa: E402

FIXTURE = HERE / "fixtures" / "ais_sample.jsonl"


def load_fixture_db() -> sqlite3.Connection:
    con = L.connect(":memory:")
    rec = R.Recorder.__new__(R.Recorder)
    rec.con, rec.buf, rec.session_id = con, [], None
    rec.stats = {"msgs": 0, "pos_new": 0, "static": 0, "skipped": 0, "errors": 0}
    R.replay(rec, FIXTURE)
    load_fixture_db.stats = rec.stats
    return con


class TestParsing(unittest.TestCase):
    def test_time(self):
        self.assertEqual(L.parse_time("2026-11-16 10:00:00.123456789 +0000 UTC"), "2026-11-16T10:00:00.123456Z")
        self.assertEqual(L.parse_time("2026-11-16 10:00:00 +0000 UTC"), "2026-11-16T10:00:00.000000Z")

    def test_position_and_static(self):
        lines = FIXTURE.read_text().splitlines()
        p = L.parse_message(lines[1])
        self.assertIsInstance(p, L.Position)
        self.assertEqual((p.mmsi, p.lat, p.lon), (111000001, 26.60, 56.55))
        s = L.parse_message(lines[0])
        self.assertIsInstance(s, L.Static)
        self.assertEqual((s.ais_type, s.length_m, s.destination), (80, 180.0, "FOR ORDERS"))
        self.assertFalse(hasattr(s, "name"), "ship names must not be stored")

    def test_skips(self):
        self.assertIsNone(L.parse_message({"MessageType": "AidsToNavigationReport",
                                           "MetaData": {"MMSI": 1, "time_utc": "2026-11-16 00:00:00 +0000 UTC"},
                                           "Message": {"AidsToNavigationReport": {}}}))
        bad = {"MessageType": "PositionReport", "MetaData": {"MMSI": 1, "time_utc": "2026-11-16 00:00:00 +0000 UTC"},
               "Message": {"PositionReport": {"Latitude": 91, "Longitude": 181}}}
        self.assertIsNone(L.parse_message(bad))

    def test_type_mapping(self):
        self.assertEqual([L.map_ship_type(t) for t in (None, 0, 70, 79, 80, 89, 52, 60)],
                         ["unknown", "unknown", "cargo", "cargo", "tanker", "tanker", "other", "other"])


class TestStore(unittest.TestCase):
    def test_replay_counts_and_no_duplicates(self):
        con = load_fixture_db()
        st = load_fixture_db.stats
        self.assertEqual(st["msgs"], 25)
        self.assertEqual(st["errors"], 1)            # the garbage line
        self.assertEqual(st["skipped"], 2)           # lat 91 + aid-to-navigation
        n = con.execute("SELECT COUNT(*) FROM positions").fetchone()[0]
        self.assertEqual(n, 16)                      # 17 valid position lines, one exact duplicate
        # replaying again (a restart overlap) adds nothing
        items = [L.parse_message(ln) for ln in FIXTURE.read_text().splitlines()[:-1]]
        n_new, _ = L.store(con, [i for i in items if i])
        self.assertEqual(n_new, 0)
        self.assertEqual(con.execute("SELECT COUNT(*) FROM positions").fetchone()[0], 16)

    def test_static_keeps_newest(self):
        con = L.connect(":memory:")
        L.store(con, [L.Static(5, "2026-11-16T02:00:00Z", 80, 1, "A", 100.0, 9.0)])
        L.store(con, [L.Static(5, "2026-11-16T01:00:00Z", 70, 1, "OLD", 100.0, 9.0)])
        self.assertEqual(con.execute("SELECT ais_type, destination FROM ship_static").fetchone(), (80, "A"))


class TestGates(unittest.TestCase):
    def test_segment_crossing(self):
        gate = ((0.0, 0.0), (1.0, 0.0))  # along the meridian lon=0, lat 0..1
        self.assertAlmostEqual(L.segment_crossing((0.5, -1.0), (0.5, 1.0), gate), 0.5)
        self.assertIsNone(L.segment_crossing((2.0, -1.0), (2.0, 1.0), gate))   # beyond the gate end
        self.assertIsNone(L.segment_crossing((0.5, 1.0), (0.5, 2.0), gate))    # same side

    def test_hormuz_crossings(self):
        con = load_fixture_db()
        cr = L.gate_crossings(con, "hormuz")
        by_ship = {}
        for mmsi, when, direction, cls in cr:
            by_ship.setdefault(mmsi, []).append((when[:10], direction, cls))
        self.assertEqual(len(by_ship[111000001]), 2)            # day 1 west, day 2 east
        self.assertNotEqual(by_ship[111000001][0][1], by_ship[111000001][1][1])
        self.assertEqual(len(by_ship[111000002]), 1)            # jitter deduplicated
        self.assertEqual(by_ship[111000003][0][2], "unknown")
        self.assertNotIn(111000004, by_ship)                    # 8 h gap not joined
        self.assertEqual(by_ship[111000005][0][2], "other")
        self.assertNotIn(222000006, by_ship)                    # other strait

        days = L.daily_counts(cr)
        self.assertEqual(days["2026-11-16"], {"tanker": 1, "cargo": 1, "unknown": 1, "other": 1,
                                              "total_pw_comparable": 2})
        self.assertEqual(days["2026-11-17"]["tanker"], 1)

    def test_bab_el_mandeb_crossing(self):
        con = load_fixture_db()
        cr = L.gate_crossings(con, "bab_el_mandeb")
        self.assertEqual([(m, c) for m, _, _, c in cr], [(222000006, "cargo")])


class TestCoverage(unittest.TestCase):
    def test_report(self):
        con = load_fixture_db()
        con.execute("INSERT INTO sessions (started_at, last_seen_at) VALUES (?, ?)",
                    ("2026-11-15T23:30:00Z", "2026-11-17T12:00:00Z"))
        self.assertEqual(L.uptime_hours_by_day(con),
                         {"2026-11-15": 0.5, "2026-11-16": 24.0, "2026-11-17": 12.0})
        pw = {"2026-11-16": {"date": "2026-11-16", "total": 5, "by_type": {"tanker": 2}}}
        rep = L.coverage_report(con, "hormuz", pw)
        days = {d["date"]: d for d in rep["days"]}
        self.assertEqual(days["2026-11-15"]["status"], "partial_day")
        self.assertEqual(days["2026-11-17"]["status"], "partial_day")
        d = days["2026-11-16"]
        self.assertEqual(d["status"], "scored")
        self.assertEqual(d["coverage_total"], 0.4)               # 2 of 5
        self.assertEqual(d["coverage_total_incl_unknown"], 0.6)  # 3 of 5
        self.assertEqual(d["coverage_tanker"], 0.5)
        self.assertEqual(d["coverage_cargo"], round(1 / 3, 3))
        self.assertTrue(rep["summary"]["below_gate"])
        self.assertIn("UNDER", rep["summary"]["verdict"])

    def test_awaiting_portwatch(self):
        con = load_fixture_db()
        con.execute("INSERT INTO sessions (started_at, last_seen_at) VALUES (?, ?)",
                    ("2026-11-16T00:00:00Z", "2026-11-17T00:00:00Z"))
        rep = L.coverage_report(con, "hormuz", {})
        self.assertEqual(rep["days"][0]["status"], "awaiting_portwatch")
        self.assertIsNone(rep["summary"])


class TestRecorderSafety(unittest.TestCase):
    def test_backoff(self):
        self.assertEqual(R.backoff_delay(0, rng=lambda: 1.0), 1.0)
        self.assertEqual(R.backoff_delay(3, rng=lambda: 1.0), 8.0)
        self.assertEqual(R.backoff_delay(20, rng=lambda: 1.0), 60.0)       # capped
        self.assertEqual(R.backoff_delay(3, rng=lambda: 0.0), 4.0)          # jitter floor

    def test_key_redacted_from_logs(self):
        secret = "sk-test-0123456789abcdef"
        stream = io.StringIO()
        h = logging.StreamHandler(stream)
        h.addFilter(R.RedactFilter(secret))
        lg = logging.getLogger("redact-test")
        lg.addHandler(h)
        lg.propagate = False
        lg.warning("subscribe failed with %s", secret)
        self.assertNotIn(secret, stream.getvalue())
        self.assertIn("***", stream.getvalue())

    def test_subscription_shape(self):
        sub = json.loads(R.subscription("k"))
        self.assertEqual(sub["APIKey"], "k")
        self.assertEqual(len(sub["BoundingBoxes"]), 2)
        for box in sub["BoundingBoxes"]:
            (a, b), (c, d) = box
            self.assertTrue(a < c and b < d)
        self.assertIn("ShipStaticData", sub["FilterMessageTypes"])

    def test_no_key_exits_cleanly(self):
        import os
        old = os.environ.pop("AISSTREAM_API_KEY", None)
        try:
            with tempfile.TemporaryDirectory() as d:
                rc = R.main(["--db", str(Path(d) / "x.sqlite"), "--hours", "0.001"])
                logging.shutdown()
            self.assertEqual(rc, 2)
        finally:
            if old is not None:
                os.environ["AISSTREAM_API_KEY"] = old

    def test_full_days_deadline(self):
        import datetime as dt
        now = dt.datetime(2026, 11, 17, 21, 0, tzinfo=dt.timezone.utc)
        # 3 h to midnight + 48 h + 10 min margin
        self.assertEqual(R.seconds_until_full_days(now, 2), (3 + 48) * 3600 + 600)

    def test_gate_inside_box(self):
        for strait, ((a, b), (c, d)) in L.GATES.items():
            (la0, lo0), (la1, lo1) = L.BOXES[strait]
            for lat, lon in ((a, b), (c, d)):
                self.assertTrue(la0 <= lat <= la1 and lo0 <= lon <= lo1, strait)


if __name__ == "__main__":
    unittest.main()
