"""Unit tests for scripts/sanctions.py (ticket #23).

    py -m unittest tests.test_sanctions -v

Synthetic fixtures in tests/fixtures/sanctions/ (invented names and numbers,
real list shapes). No network; the AIS store is a temporary SQLite file.
"""
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "scripts"))
import ais_lib as L  # noqa: E402
import sanctions as S  # noqa: E402

FX = HERE / "fixtures" / "sanctions"
META = {"url": "https://example.invalid/list.xml", "fetched_at": "2026-11-18T00:00:00Z"}


class TestImo(unittest.TestCase):
    def test_check_digit(self):
        self.assertTrue(S.imo_check_ok("1234567"))     # invented, valid check digit
        self.assertTrue(S.imo_check_ok("2222224"))
        self.assertFalse(S.imo_check_ok("1234568"))    # wrong check digit
        for bad in ("123456", "12345670", "IMO9074", "", None, "123456x"):
            self.assertFalse(S.imo_check_ok(bad), bad)

    def test_formats(self):
        for text in ("IMO 1234567", "IMO1234567", "imo: 1234567", "IMO No. 1234567", "1234567", " 1234567 "):
            self.assertEqual(S.parse_imos(text), [("1234567", True)], text)
        self.assertEqual(S.parse_imos("IMO 123456"), [("123456", False)])        # missing digit: kept, invalid
        self.assertEqual(S.parse_imos("IMO 1234568"), [("1234568", False)])
        self.assertEqual(S.parse_imos("IMO 1234567; IMO 2222224; IMO 1234567"),
                         [("1234567", True), ("2222224", True)])               # de-duplicated
        self.assertEqual(S.parse_imos(None), [])
        self.assertEqual(S.parse_imos("Panama"), [])
        self.assertEqual(S.parse_imos("Reg. 12345678901"), [])

    def test_xml_complete(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "x.xml"
            p.write_text('<?xml version="1.0"?>\n<!-- c -->\n<root a="1"><x/></root>\n')
            S.check_xml_complete(p)
            p.write_text('<?xml version="1.0"?>\n<root a="1"><x/>')
            with self.assertRaises(IOError):
                S.check_xml_complete(p)


class TestParsers(unittest.TestCase):
    def test_ofac(self):
        dates = S.parse_ofac_listing_dates(FX / "ofac_sdn_adv.xml")
        self.assertEqual(dates, {"102": "2025-02-24", "103": "1996-09-01"})
        rows, meta = S.parse_ofac(FX / "ofac_sdn.xml", "ofac_sdn", dates)
        self.assertEqual(meta["publish_date"], "2026-11-13")
        by = {r.entry_id: r for r in rows}
        self.assertEqual(set(by), {"102", "103", "104"}, "the Entity row with a company number is not a vessel")
        t = by["102"]
        self.assertEqual((t.imo, t.flag, t.owner, t.programme, t.listed_on, t.mmsi),
                         ("1234567", "Panama", "EXAMPLE SHIPPING CO", "IRAN; SDGT", "2025-02-24", "111222333"))
        self.assertEqual(t.names, ["TEST TANKER ONE", "OLD TEST NAME"])
        self.assertEqual((by["103"].imo, by["103"].imo_raw, by["103"].owner), (None, "1234568", "Example Owner S.A."))
        self.assertEqual((by["104"].imo, by["104"].imo_raw, by["104"].listed_on), (None, "", None))

    def test_uk(self):
        rows, meta = S.parse_uk(FX / "uk.xml")
        self.assertEqual(meta["publish_date"], "2026-11-17")
        self.assertEqual([(r.entry_id, r.imo) for r in rows], [("RUS9001", "1234567"), ("RUS9002", "2222224")])
        r = rows[0]
        self.assertEqual((r.flag, r.owner, r.listed_on, r.last_updated),
                         ("Gabon", "Example Ship Management Ltd", "2025-05-09", "2026-10-01"))
        self.assertIn("Russia", r.programme)

    def test_eu(self):
        rows, meta = S.parse_eu(FX / "eu.xml")
        self.assertEqual(meta["publish_date"], "2026-10-23")
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual((r.kind, r.imo_raw, r.imo, r.listed_on, r.programme),
                         ("company", "5555555", None, "2016-03-01", "PRK"))

    def test_eu_833_versions(self):
        v = S.eu_833_versions((FX / "eu_833_index.html").read_text(encoding="utf-8"))
        self.assertEqual(v, {"celex": "02014R0833-20260724", "version_date": "2026-07-24",
                             "latest_amendment_date": "2026-10-20", "latest_amendment_celex": "32026R2100"})
        with self.assertRaises(ValueError):
            S.eu_833_versions("<html>nothing here</html>")

    def test_eu_lag_warning(self):
        # the fixture's newest amending act (20 Oct) is newer than its text (24 Jul): warn
        v = S.eu_833_versions((FX / "eu_833_index.html").read_text(encoding="utf-8"))
        self.assertIn("32026R2100", S.eu_lag_warning(v))
        self.assertIsNone(S.eu_lag_warning({**v, "latest_amendment_date": "2026-07-24"}))
        self.assertIsNone(S.eu_lag_warning({**v, "latest_amendment_date": None}))   # not parsed: no false alarm

    def test_eu_vessels(self):
        rows, meta = S.parse_eu_vessels(FX / "eu_vessels.html", {"version_date": "2026-07-24"})
        self.assertEqual(meta["publish_date"], "2026-07-24")
        # only Annex XLII rows: XLI and XLIII tables are ignored; deleted row 2 is a gap
        self.assertEqual([(r.entry_id, r.imo, r.imo_raw) for r in rows],
                         [("XLII-1", "1234567", "1234567"), ("XLII-3", "2222224", "2222224"),
                          ("XLII-4", None, "1234568")])
        one, three = rows[0], rows[1]
        self.assertEqual((one.names, one.listed_on, one.programme),
                         (["Test Tanker One"], "2024-06-25", "EU Reg. 833/2014 Annex XLII, Art. 3s(2)(b)"))
        self.assertEqual(three.names, ["Test Carrier Two", "Old Name A", "Old Name B"])
        self.assertEqual((three.listed_on, three.programme[-3:]), ("2026-08-13", "(c)"))   # odd 'paragraph 2(2)' form

    def test_eu_vessels_layout_change_fails_loudly(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "x.html"
            p.write_text("<html><body><p>no annex</p></body></html>")
            with self.assertRaises(ValueError):
                S.parse_eu_vessels(p)
            p.write_text('<html><body><div id="anx_XLII"><table></table></div></body></html>')
            with self.assertRaises(ValueError):
                S.parse_eu_vessels(p)

    def test_html_complete(self):
        S.check_html_complete(FX / "eu_vessels.html")
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "cut.html"
            p.write_text("<html><body><table><tr><td>1.</td>")
            with self.assertRaises(IOError):
                S.check_html_complete(p)


class TestStoreAndMatch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        d = Path(self.tmp.name)
        self.con = S.connect(d / "vessels.sqlite")
        self.addCleanup(self.con.close)
        dates = S.parse_ofac_listing_dates(FX / "ofac_sdn_adv.xml")
        for lid, (rows, meta) in {"ofac_sdn": S.parse_ofac(FX / "ofac_sdn.xml", "ofac_sdn", dates),
                                  "uk": S.parse_uk(FX / "uk.xml"), "eu_fsf": S.parse_eu(FX / "eu.xml"),
                                  "eu_vessels": S.parse_eu_vessels(FX / "eu_vessels.html")}.items():
            S.store(self.con, "2026-11-18", lid, rows, {**META, **meta})
        # a small AIS store in the recorder's own schema
        self.ais_path = d / "ais.sqlite"
        ais = L.connect(self.ais_path)
        ais.executemany("INSERT INTO ship_static (mmsi, ais_type, imo, destination, length_m, draught_m, updated_at) "
                        "VALUES (?,?,?,?,?,?,?)", [
            (200000001, 80, 1234567, None, 250.0, 15.0, "2026-11-18T00:00:00Z"),   # listed (OFAC + UK)
            (200000002, 70, 2222224, None, 180.0, 10.0, "2026-11-18T00:00:00Z"),   # listed (UK)
            (200000003, 80, 1234568, None, 200.0, 12.0, "2026-11-18T00:00:00Z"),   # bad check digit
            (200000004, 70, 3333331, None, 150.0, 8.0, "2026-11-18T00:00:00Z"),    # valid, not listed
            (200000005, 70, None, None, 90.0, 5.0, "2026-11-18T00:00:00Z"),        # no IMO
        ])
        ais.execute("INSERT INTO positions (mmsi, ts, lat, lon) VALUES (111222333, '2026-11-18T01:00:00Z', 26.5, 56.4)")
        ais.commit()
        ais.close()

    def test_counts(self):
        rows = dict((r[0], r[1:]) for r in self.con.execute(
            "SELECT list_id, n_entries, n_vessels, n_valid_imo, source_url, fetched_at FROM lists"))
        self.assertEqual(rows["ofac_sdn"][:3], (3, 3, 1))
        self.assertEqual(rows["uk"][:3], (2, 2, 2))
        self.assertEqual(rows["eu_fsf"][:3], (1, 0, 0))
        self.assertEqual(rows["eu_vessels"][:3], (3, 3, 2))
        self.assertTrue(all(r[3] == META["url"] and r[4] for r in rows.values()))

    def test_restore_same_day_replaces(self):
        rows, meta = S.parse_uk(FX / "uk.xml")
        S.store(self.con, "2026-11-18", "uk", rows, {**META, **meta})
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM vessels WHERE list_id='uk'").fetchone()[0], 2)
        S.store(self.con, "2026-11-19", "uk", rows, {**META, **meta})
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM vessels WHERE list_id='uk'").fetchone()[0], 4)

    def test_match_read_only(self):
        ais, mmsis = S.read_ais(self.ais_path)
        self.assertEqual(len(ais), 4)
        self.assertIn(111222333, mmsis)
        res = S.match(self.con, "2026-11-18", ais, mmsis)
        self.assertEqual(res["ais_valid_imo"], 3)
        self.assertEqual(sorted((m, i, l) for m, i, l in res["matches"]),
                         [(200000001, "1234567", ["eu_vessels", "ofac_sdn", "uk"]), (200000002, "2222224", ["eu_vessels", "uk"])])
        self.assertEqual(res["mmsi_matches"], [(111222333, ["ofac_sdn"])])
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM matches").fetchone()[0], 3)
        # the AIS store must not be writable through the script's connection mode
        con = sqlite3.connect(f"file:{self.ais_path.as_posix()}?mode=ro", uri=True)
        with self.assertRaises(sqlite3.OperationalError):
            con.execute("DELETE FROM ship_static")
        con.close()

    def test_missing_ais_db(self):
        self.assertEqual(S.read_ais(Path(self.tmp.name) / "nope.sqlite"), ([], set()))


if __name__ == "__main__":
    unittest.main()
