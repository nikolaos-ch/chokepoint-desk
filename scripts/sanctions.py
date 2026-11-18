"""Sanctions-list vessel extraction and private IMO match (ticket #23).

    py scripts/sanctions.py              # download all lists, rebuild the table, match
    py scripts/sanctions.py --offline    # re-parse the cached downloads, match
    py scripts/sanctions.py --no-match   # skip the AIS match

Downloads the official free lists, extracts every vessel entry with its IMO
number, and stores a dated table in var/sanctions/vessels.sqlite. Then reads
the AIS recorder's ship_static.imo READ-ONLY and prints a private summary.

PRIVATE: var/ is gitignored. Nothing here is published or deployed: no ship
names or matched IMOs ever go to data/ or the site (Plan 2, responsible design).

Lists (see README "Sanctions lists (private)" for terms of use):
  ofac_sdn   OFAC Specially Designated Nationals list (legacy XML: vessel details)
             + SDN_ADVANCED.XML, read only for each entry's listing date
  ofac_cons  OFAC Consolidated (non-SDN) list
  uk         UK Sanctions List (FCDO), XML
  eu_fsf     EU consolidated financial sanctions list (FSF), XML. It holds no
             vessel subjects; IMO numbers there belong to shipping COMPANIES and
             are kept with kind='company' (not matchable against AIS).
  eu_vessels EU vessel list: Annex XLII to Council Regulation (EU) 833/2014
             (Article 3s port-access and services ban, the 'shadow fleet' list),
             from the latest consolidated text on EUR-Lex (HTML). EUR-Lex's own
             'Amended by' list is checked: a run warns when an amending act is
             newer than the consolidated version, i.e. the text may be behind.

Stdlib only.
"""
from __future__ import annotations

import argparse
import datetime as dt
import gzip
import hashlib
import http.client
import json
import re
import sqlite3
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "var" / "sanctions"            # gitignored (var/)
RAW_DIR = OUT_DIR / "raw"
DB_PATH = OUT_DIR / "vessels.sqlite"
AIS_DB = ROOT / "var" / "ais" / "ais.sqlite"

OFAC = "https://sanctionslistservice.ofac.treas.gov/api/PublicationPreview/exports/"
SOURCES = {
    "ofac_sdn": OFAC + "SDN.XML",
    "ofac_sdn_adv": OFAC + "SDN_ADVANCED.XML",   # listing dates only
    "ofac_cons": OFAC + "CONSOLIDATED.XML",
    "uk": "https://sanctionslist.fcdo.gov.uk/docs/UK-Sanctions-List.xml",
    "eu_fsf": ("https://webgate.ec.europa.eu/fsd/fsf/public/files/"
               "xmlFullSanctionsList_1_1/content?token=dG9rZW4tMjAxNw"),
}
EURLEX = "https://eur-lex.europa.eu/legal-content/EN/"
EU_833_INDEX = EURLEX + "ALL/?uri=CELEX:32014R0833"            # versions + 'Amended by'
EU_833_CONSOLIDATED = EURLEX + "TXT/HTML/?uri=CELEX:{celex}"    # e.g. 02014R0833-20260724
HTML_SOURCES = ("eu_833_index", "eu_vessels")                   # fetched after SOURCES, see build()
LIST_NAMES = {
    "ofac_sdn": "OFAC SDN List",
    "ofac_cons": "OFAC Consolidated (non-SDN) List",
    "uk": "UK Sanctions List (FCDO)",
    "eu_fsf": "EU Consolidated Financial Sanctions List (FSF)",
    "eu_vessels": "EU vessel list, Annex XLII to Regulation (EU) 833/2014 (EUR-Lex consolidated)",
}
USER_AGENT = "chokepoint-desk-research/1.0 (private sanctions check; python-urllib)"


# ------------------------------------------------------------------ IMO numbers
IMO_RE = re.compile(r"IMO\s*(?:No\.?|Number|#)?\s*[:.]?\s*(\d{5,8})", re.I)


def imo_check_ok(imo: str) -> bool:
    """Ship IMO number: 7 digits; the first six weighted 7..2, the sum's last
    digit is the seventh. Invented example 1234567: 1*7+2*6+3*5+4*4+5*3+6*2 = 77 -> 7."""
    if not (isinstance(imo, str) and len(imo) == 7 and imo.isdigit()):
        return False
    return sum(int(d) * w for d, w in zip(imo[:6], range(7, 1, -1))) % 10 == int(imo[6])


def parse_imos(text: str | None) -> list[tuple[str, bool]]:
    """All IMO numbers in a free-text field -> [(digits, valid)].

    Accepts 'IMO 1234567', 'IMO1234567', 'IMO: 1234567', 'IMO No. 1234567' and a
    bare 7-digit number (the UK and EU fields hold the number alone). A number
    with a missing or wrong check digit (6 digits, or a 7-digit number that
    fails the check) is returned with valid=False: kept for the record, never
    matched, and never 'repaired' by guessing a digit."""
    if not text:
        return []
    found = [m.group(1) for m in IMO_RE.finditer(text)]
    if not found:
        bare = re.fullmatch(r"\s*(\d{5,8})\s*", text)
        found = [bare.group(1)] if bare else []
    out, seen = [], set()
    for n in found:
        if n not in seen:
            seen.add(n)
            out.append((n, imo_check_ok(n)))
    return out


# ------------------------------------------------------------------ records
@dataclass
class Entry:
    list_id: str
    entry_id: str
    kind: str                       # 'vessel' or 'company' (owner IMO number)
    imo_raw: str
    imo: str | None                 # 7 digits when the check digit is valid, else None
    names: list[str] = field(default_factory=list)
    flag: str | None = None
    owner: str | None = None
    programme: str | None = None
    listed_on: str | None = None    # ISO date the entry was first listed, when the list says
    last_updated: str | None = None
    mmsi: str | None = None         # MMSI the list gives (OFAC only); weaker than IMO


def _entries(list_id, entry_id, kind, imo_texts, **kw) -> list[Entry]:
    """One Entry per IMO number found; an entry with no IMO at all is kept once
    with imo_raw '' so the vessel counts stay complete."""
    pairs = [p for t in imo_texts for p in parse_imos(t)]
    if not pairs:
        return [Entry(list_id, entry_id, kind, "", None, **kw)]
    return [Entry(list_id, entry_id, kind, n, n if ok else None, **kw) for n, ok in pairs]


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child(el, name):
    for c in el:
        if _local(c.tag) == name:
            return c
    return None


def _children(el, name):
    return [c for c in el if _local(c.tag) == name]


def _kids(el, parent, name):
    """Children called `name` of el's child `parent` ([] when either is absent)."""
    p = _child(el, parent) if el is not None else None
    return [] if p is None else _children(p, name)


def _text(el, *path) -> str | None:
    for p in path:
        if el is None:
            return None
        el = _child(el, p)
    if el is None or el.text is None:
        return None
    return el.text.strip() or None


def _dmy(s: str | None) -> str | None:
    """'03/10/2017' (UK, day first) -> '2017-10-03'."""
    if not s:
        return None
    try:
        return dt.datetime.strptime(s.strip(), "%d/%m/%Y").date().isoformat()
    except ValueError:
        return None


# ------------------------------------------------------------------ OFAC
def parse_ofac(path: Path, list_id: str, listed_on: dict[str, str] | None = None) -> tuple[list[Entry], dict]:
    """OFAC legacy XML (SDN.XML / CONSOLIDATED.XML): sdnType 'Vessel' entries.
    IMO numbers sit in idList as idType 'Vessel Registration Identification',
    idNumber 'IMO 1234567'. Entity rows with an IMO *company* number are not
    vessels and are skipped."""
    listed_on = listed_on or {}
    out, meta = [], {}
    for _, el in ET.iterparse(path, events=("end",)):
        tag = _local(el.tag)
        if tag == "Publish_Date":
            try:
                meta["publish_date"] = dt.datetime.strptime(el.text.strip(), "%m/%d/%Y").date().isoformat()
            except (ValueError, AttributeError):
                pass
        if tag != "sdnEntry":
            continue
        if _text(el, "sdnType") == "Vessel":
            uid = _text(el, "uid")
            names = [_text(el, "lastName")] + [_text(a, "lastName") for a in _kids(el, "akaList", "aka")]
            idl = _kids(el, "idList", "id")
            ids = [_text(i, "idNumber") for i in idl if (_text(i, "idType") or "").startswith("Vessel Registration")]
            mmsis = [_text(i, "idNumber") for i in idl if _text(i, "idType") == "MMSI"]
            linked = re.findall(r"Linked To: ([^)]+)\)", _text(el, "remarks") or "")
            vi = _child(el, "vesselInfo")
            progs = [p.text.strip() for p in _kids(el, "programList", "program") if p.text]
            out += _entries(list_id, uid, "vessel", ids,
                            names=[n for n in names if n], flag=_text(vi, "vesselFlag"),
                            owner=_text(vi, "vesselOwner") or "; ".join(linked) or None,
                            programme="; ".join(progs) or None, listed_on=listed_on.get(uid),
                            mmsi=next((m for m in mmsis if m and m.isdigit() and len(m) == 9), None))
        el.clear()
    return out, meta


def parse_ofac_listing_dates(path: Path) -> dict[str, str]:
    """SDN_ADVANCED.XML -> {profile id (= legacy uid): listing date}. Each
    SanctionsEntry has an EntryEvent (type 1, 'Created') with the date the
    entry was added. Streamed: the file is ~130 MB."""
    dates: dict[str, str] = {}
    for _, el in ET.iterparse(path, events=("end",)):
        if _local(el.tag) != "SanctionsEntry":
            continue
        ev = _child(el, "EntryEvent")
        d = _child(ev, "Date") if ev is not None else None
        if d is not None:
            try:
                y, m, day = (int(_text(d, k)) for k in ("Year", "Month", "Day"))
                dates[el.get("ProfileID")] = dt.date(y, m, day).isoformat()
            except (TypeError, ValueError):
                pass
        el.clear()
    return dates


# ------------------------------------------------------------------ UK
def parse_uk(path: Path) -> tuple[list[Entry], dict]:
    """UK Sanctions List XML: Designation rows with IndividualEntityShip 'Ship'."""
    out, meta = [], {}
    for _, el in ET.iterparse(path, events=("end",)):
        tag = _local(el.tag)
        if tag == "DateGenerated":
            meta["publish_date"] = _dmy(el.text)
        if tag != "Designation":
            continue
        if (_text(el, "IndividualEntityShip") or "").lower() == "ship":
            names = [_text(n, "Name6") for n in _kids(el, "Names", "Name")]
            ships = _kids(el, "ShipDetails", "Ship")
            imos = [i.text for s in ships for i in _kids(s, "IMONumbers", "IMONumber") if i.text]
            flags = [f.text.strip() for s in ships
                     for f in _kids(s, "CurrentBelievedFlagOfShips", "CurrentBelievedFlagOfShip") if f.text]
            owners = [o.text.strip() for s in ships
                      for o in _kids(s, "CurrentOwnerOperators", "CurrentOwnerOperator") if o.text]
            out += _entries("uk", _text(el, "UniqueID"), "vessel", imos,
                            names=[n for n in names if n], flag="; ".join(flags) or None,
                            owner="; ".join(owners) or None, programme=_text(el, "RegimeName"),
                            listed_on=_dmy(_text(el, "DateDesignated")),
                            last_updated=_dmy(_text(el, "LastUpdated")))
        el.clear()
    return out, meta


# ------------------------------------------------------------------ EU
def parse_eu(path: Path) -> tuple[list[Entry], dict]:
    """EU FSF XML. Any sanctionEntity that is not a person or enterprise is
    treated as a vessel; identifications typed 'imo' on an enterprise are the
    company's IMO owner number (kind='company'). Listing date = the earliest
    regulation publicationDate on the entry."""
    out, meta = [], {}
    for ev, el in ET.iterparse(path, events=("start", "end")):
        tag = _local(el.tag)
        if ev == "start":
            if tag == "export":
                meta["publish_date"] = (el.get("generationDate") or "")[:10] or None
            continue
        if tag != "sanctionEntity":
            continue
        st = _child(el, "subjectType")
        code = (st.get("code") if st is not None else "") or ""
        imos = [i.get("number") for i in _children(el, "identification")
                if (i.get("identificationTypeCode") or "").lower() == "imo" and i.get("number")]
        if code not in ("person", "enterprise") or imos:
            kind = "company" if code == "enterprise" else ("vessel" if code != "person" else "person")
            if kind != "person":
                regs = _children(el, "regulation")
                pubs = sorted(r.get("publicationDate") for r in regs if r.get("publicationDate"))
                progs = sorted({r.get("programme") for r in regs if r.get("programme")})
                names = [a.get("wholeName") for a in _children(el, "nameAlias") if a.get("wholeName")]
                found = _entries("eu_fsf", el.get("euReferenceNumber") or el.get("logicalId"), kind, imos,
                                 names=names, programme="; ".join(progs) or None,
                                 listed_on=pubs[0] if pubs else None)
                if kind == "company":   # owner numbers use another check scheme: never matched
                    for f in found:
                        f.imo = None
                out += found
        el.clear()
    return out, meta


# ------------------------------------------------------------------ EU Annex XLII
def eu_833_versions(index_html: str) -> dict:
    """From EUR-Lex's ALL page for 32014R0833: the newest consolidated version
    (CELEX 02014R0833-YYYYMMDD) and the newest amending act listed under
    'Amended by' (CELEX 3YYYYRNNNN with its date)."""
    versions = sorted(set(re.findall(r"02014R0833-(\d{8})", index_html)))
    if not versions:
        raise ValueError("no consolidated version of Regulation 833/2014 found on EUR-Lex")
    v = versions[-1]
    amend = []
    # each 'Amended by' / 'Modified by' row: <a data-celex="3...">, then a
    # <td data-sort="YYYYMMDD"> holding the act's date a few cells later
    for m in re.finditer(r'data-celex="(3\d{4}R\d{4})"', index_html):
        d = re.search(r'data-sort="(\d{8})"', index_html[m.end():m.end() + 800])
        if d:
            ds = d.group(1)
            amend.append((f"{ds[:4]}-{ds[4:6]}-{ds[6:]}", m.group(1)))
    latest = max(amend) if amend else (None, None)
    return {"celex": f"02014R0833-{v}", "version_date": f"{v[:4]}-{v[4:6]}-{v[6:]}",
            "latest_amendment_date": latest[0], "latest_amendment_celex": latest[1]}


def eu_lag_warning(ev: dict) -> str | None:
    """A warning when EUR-Lex lists an amending act newer than the consolidated
    text that was read (Annex XLII may then lack recent listings), else None."""
    if ev.get("latest_amendment_date") and ev.get("version_date") and \
            ev["latest_amendment_date"] > ev["version_date"]:
        return (f"WARNING: EUR-Lex lists amending act {ev['latest_amendment_celex']} of "
                f"{ev['latest_amendment_date']}, newer than the consolidated text of {ev['version_date']}: "
                f"Annex XLII may be missing recent listings until EUR-Lex consolidates it.")
    return None


class _TableRows(HTMLParser):
    """Collect <tr> rows as lists of cell texts; a <p> or <br> inside a cell
    becomes a line break so '(formerly ...)' stays separable from the name."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows, self.row, self.cell = [], None, None

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self.row = []
        elif tag == "td" and self.row is not None:
            self.cell = []
        elif tag in ("p", "br") and self.cell is not None:
            self.cell.append("\n")

    def handle_endtag(self, tag):
        if tag == "td" and self.cell is not None:
            text = "\n".join(line.strip() for line in "".join(self.cell).splitlines())
            self.row.append(re.sub(r"\n{2,}", "\n", text).strip())
            self.cell = None
        elif tag == "tr" and self.row is not None:
            self.rows.append(self.row)
            self.row = None

    def handle_data(self, data):
        if self.cell is not None:
            self.cell.append(data)


def _eu_names(cell: str) -> list[str]:
    """'Test Ship\n(formerly Old Name A; Old Name B)' -> ['Test Ship', 'Old Name A', 'Old Name B']
    (invented names)."""
    flat = " ".join(cell.split())
    names = [re.sub(r"\s*\(formerly[^)]*\)", "", flat, flags=re.I).strip()]
    for group in re.findall(r"\(formerly\s+([^)]*)\)", flat, re.I):
        names += [n.strip() for n in group.split(";") if n.strip()]
    return [n for n in names if n]


def _eu_date(s: str) -> str | None:
    """'25.6.2024' -> '2024-06-25'."""
    m = re.fullmatch(r"\s*(\d{1,2})\.(\d{1,2})\.(\d{4})\s*", s or "")
    return dt.date(int(m.group(3)), int(m.group(2)), int(m.group(1))).isoformat() if m else None


def parse_eu_vessels(path: Path, version: dict | None = None) -> tuple[list[Entry], dict]:
    """Annex XLII of the consolidated Regulation 833/2014 (EUR-Lex HTML): rows
    'N. | vessel name (formerly ...) | IMO | grounds | date of application'.
    Deleted entries leave a numbered gap and are simply absent. Raises if the
    annex is missing or yields no vessels, so a page change can't silently
    empty the list."""
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    a = text.find('id="anx_XLII"')
    b = text.find('id="anx_XLIII"', a + 1)
    if a < 0:
        raise ValueError("Annex XLII not found in the EUR-Lex text")
    p = _TableRows()
    p.feed(text[a:b if b > 0 else len(text)])
    out = []
    for row in p.rows:
        if len(row) < 4 or not re.fullmatch(r"\d+\.", row[0].strip()):
            continue
        num = row[0].strip().rstrip(".")
        grounds = " ".join(row[3].split()) if len(row) > 4 else ""
        # usually 'Article 3s(2), point (b):'; one row reads 'Article 3s, paragraph 2(2), point (b)'
        point = re.search(r"Article 3s\b.*?point \((\w)\)", grounds)
        out += _entries("eu_vessels", f"XLII-{num}", "vessel", [row[2]],
                        names=_eu_names(row[1]),
                        programme="EU Reg. 833/2014 Annex XLII, Art. 3s(2)"
                                  + (f"({point.group(1)})" if point else ""),
                        listed_on=_eu_date(row[-1]))
    if not out:
        raise ValueError("Annex XLII parsed to 0 vessels: the EUR-Lex layout may have changed")
    return out, {"publish_date": (version or {}).get("version_date")}


# ------------------------------------------------------------------ download
def check_html_complete(path: Path) -> None:
    with open(path, "rb") as f:
        f.seek(0, 2)
        f.seek(max(0, f.tell() - 512))
        tail = f.read().decode("utf-8", "replace").rstrip().lower()
    if not tail.endswith("</html>"):
        raise IOError(f"{path.name}: truncated (does not end with </html>)")


def check_xml_complete(path: Path) -> None:
    """Raise if the file does not end with its root element's closing tag.
    Long downloads from these servers have ended early without any error, so
    a cut-off file must never replace the cached copy."""
    with open(path, "rb") as f:
        head = f.read(8192).decode("utf-8", "replace")
        f.seek(0, 2)
        f.seek(max(0, f.tell() - 512))
        tail = f.read().decode("utf-8", "replace").rstrip()
    m = re.search(r"<([A-Za-z_][\w.:-]*)[\s>/]", re.sub(r"<[?!][^>]*>", "", head))
    if not m:
        raise IOError(f"{path.name}: no XML root element")
    if not tail.endswith(f"</{m.group(1)}>"):
        raise IOError(f"{path.name}: truncated (does not end with </{m.group(1)}>)")


def _fetch_resumable(url: str, tmp: Path, max_resumes: int = 20) -> int:
    """Download url to tmp, resuming with HTTP Range when the server closes the
    connection early (seen on OFAC's ~130 MB file). If-Range on the first
    response's ETag / Last-Modified makes a changed file restart from zero
    instead of mixing two versions. Returns the byte count."""
    n, expected, validator = 0, None, None
    open(tmp, "wb").close()
    for _ in range(max_resumes + 1):
        headers = {"User-Agent": USER_AGENT}
        if n:
            headers["Range"] = f"bytes={n}-"
            if validator:
                headers["If-Range"] = validator
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=300) as r:
            if n and r.status != 206:          # no resume support, or the file changed
                n, expected = 0, None
                open(tmp, "wb").close()
            if not n:
                cl = r.headers.get("Content-Length")
                expected = int(cl) if cl else None
                validator = r.headers.get("ETag") or r.headers.get("Last-Modified")
            with open(tmp, "ab") as f:
                try:
                    while chunk := r.read(1 << 20):
                        f.write(chunk)
                        n += len(chunk)
                except (OSError, http.client.HTTPException):
                    if expected is None:       # nothing to resume against
                        raise
        if expected is None or n >= expected:
            break
        print(f"    resuming at {n:,} of {expected:,} bytes", file=sys.stderr)
    if expected is not None and n != expected:
        raise IOError(f"got {n} of {expected} bytes")
    return n


def _fetch_gzip(url: str, tmp: Path) -> int:
    """One gzip-encoded GET (EUR-Lex). Its chunked plain responses break off
    mid-way with no length to resume against; gzip is ~2.5x smaller and its
    trailer (CRC + length) makes gzip.decompress fail on a cut-off body."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept-Encoding": "gzip"})
    with urllib.request.urlopen(req, timeout=300) as r:
        body = r.read()
        if (r.headers.get("Content-Encoding") or "").lower() == "gzip":
            body = gzip.decompress(body)
    tmp.write_bytes(body)
    return len(body)


def download(name: str, url: str, dest: Path, tries: int = 3, check=None, use_gzip: bool = False) -> dict:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    for attempt in range(1, tries + 1):
        try:
            n = _fetch_gzip(url, tmp) if use_gzip else _fetch_resumable(url, tmp)
            if n < 1000:
                raise IOError(f"only {n} bytes")
            (check or check_xml_complete)(tmp)
            h = hashlib.sha256()
            with open(tmp, "rb") as f:
                while chunk := f.read(1 << 20):
                    h.update(chunk)
            tmp.replace(dest)
            return {"url": url, "fetched_at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
                    .isoformat().replace("+00:00", "Z"), "bytes": n, "sha256": h.hexdigest()}
        except Exception as e:  # noqa: BLE001 - retried, then re-raised
            print(f"  {name}: attempt {attempt} failed: {e}", file=sys.stderr)
            if attempt == tries:
                tmp.unlink(missing_ok=True)
                raise
            time.sleep(5 * attempt)


# ------------------------------------------------------------------ storage
SCHEMA = """
CREATE TABLE IF NOT EXISTS lists (
  snapshot TEXT NOT NULL, list_id TEXT NOT NULL, list_name TEXT NOT NULL,
  source_url TEXT NOT NULL, fetched_at TEXT NOT NULL, publish_date TEXT,
  bytes INTEGER, sha256 TEXT, n_entries INTEGER, n_vessels INTEGER, n_valid_imo INTEGER,
  PRIMARY KEY (snapshot, list_id)
);
CREATE TABLE IF NOT EXISTS vessels (
  snapshot TEXT NOT NULL, list_id TEXT NOT NULL, entry_id TEXT NOT NULL, kind TEXT NOT NULL,
  imo TEXT, imo_raw TEXT NOT NULL, imo_valid INTEGER NOT NULL,
  names TEXT, flag TEXT, owner TEXT, programme TEXT, listed_on TEXT, last_updated TEXT,
  listed_mmsi TEXT
);
CREATE INDEX IF NOT EXISTS vessels_imo ON vessels (snapshot, imo);
CREATE TABLE IF NOT EXISTS matches (
  snapshot TEXT NOT NULL, matched_at TEXT NOT NULL, matched_on TEXT NOT NULL,  -- 'imo' or 'mmsi'
  imo TEXT, mmsi INTEGER NOT NULL, lists TEXT NOT NULL
);
"""


def connect(path: Path = DB_PATH) -> sqlite3.Connection:
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path))
    con.executescript(SCHEMA)
    return con


def store(con, snapshot: str, list_id: str, entries: list[Entry], meta: dict) -> dict:
    """Replace one list's rows for this snapshot date (a re-run on the same day
    overwrites it; earlier days are kept as history)."""
    vessels = [e for e in entries if e.kind == "vessel"]
    row = {"n_entries": len({e.entry_id for e in entries}),
           "n_vessels": len({e.entry_id for e in vessels}),
           "n_valid_imo": len({e.imo for e in vessels if e.imo})}
    with con:
        con.execute("DELETE FROM vessels WHERE snapshot=? AND list_id=?", (snapshot, list_id))
        con.execute("DELETE FROM lists WHERE snapshot=? AND list_id=?", (snapshot, list_id))
        con.executemany("INSERT INTO vessels VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", [
            (snapshot, e.list_id, e.entry_id, e.kind, e.imo, e.imo_raw, int(e.imo is not None),
             json.dumps(e.names, ensure_ascii=False), e.flag, e.owner, e.programme, e.listed_on, e.last_updated,
             e.mmsi)
            for e in entries])
        con.execute("INSERT INTO lists VALUES (?,?,?,?,?,?,?,?,?,?,?)", (
            snapshot, list_id, LIST_NAMES[list_id], meta["url"], meta["fetched_at"], meta.get("publish_date"),
            meta.get("bytes"), meta.get("sha256"), row["n_entries"], row["n_vessels"], row["n_valid_imo"]))
    return row


# ------------------------------------------------------------------ AIS match
def read_ais(path: Path = AIS_DB) -> tuple[list[tuple[int, str]], set[int]]:
    """([(mmsi, imo)] from ship_static, {every MMSI seen}), opened READ-ONLY.
    SQLite WAL readers never block the recorder's writer; two short queries,
    then the handle is closed."""
    if not Path(path).exists():
        return [], set()
    con = sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True, timeout=5)
    try:
        con.execute("PRAGMA query_only=1")
        rows = con.execute("SELECT mmsi, imo FROM ship_static WHERE imo IS NOT NULL AND imo > 0").fetchall()
        mmsis = {m for (m,) in con.execute("SELECT mmsi FROM ship_static UNION SELECT DISTINCT mmsi FROM positions")}
    finally:
        con.close()
    return [(int(m), str(int(i)).zfill(7)) for m, i in rows], mmsis


def match(con, snapshot: str, ais: list[tuple[int, str]], ais_mmsis: set[int] = frozenset()) -> dict:
    """IMO match (primary) plus an MMSI match for ships whose AIS IMO is unknown
    or not listed. MMSIs are reassigned when a ship changes flag, so an MMSI-only
    hit is a lead to check, not an identification."""
    listed: dict[str, set[str]] = {}
    by_mmsi: dict[int, set[str]] = {}
    for imo, lid, mmsi in con.execute(
            "SELECT DISTINCT imo, list_id, listed_mmsi FROM vessels WHERE snapshot=? AND kind='vessel'", (snapshot,)):
        if imo:
            listed.setdefault(imo, set()).add(lid)
        if mmsi:
            by_mmsi.setdefault(int(mmsi), set()).add(lid)
    valid = [(m, i) for m, i in ais if imo_check_ok(i)]
    hits = [(m, i, sorted(listed[i])) for m, i in valid if i in listed]
    imo_hit_mmsis = {m for m, _, _ in hits}
    mmsi_hits = [(m, sorted(by_mmsi[m])) for m in sorted(ais_mmsis) if m in by_mmsi and m not in imo_hit_mmsis]
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    with con:
        con.execute("DELETE FROM matches WHERE snapshot=?", (snapshot,))
        con.executemany("INSERT INTO matches VALUES (?,?,?,?,?,?)",
                        [(snapshot, now, "imo", i, m, ",".join(l)) for m, i, l in hits]
                        + [(snapshot, now, "mmsi", None, m, ",".join(l)) for m, l in mmsi_hits])
    return {"ais_ships_with_imo": len(ais), "ais_valid_imo": len(valid),
            "ais_unique_valid_imo": len({i for _, i in valid}), "ais_mmsis": len(ais_mmsis),
            "listed_mmsis": len(by_mmsi), "matches": hits, "mmsi_matches": mmsi_hits}


# ------------------------------------------------------------------ main
def raw_path(name: str) -> Path:
    return RAW_DIR / f"{name}.{'html' if name in HTML_SOURCES else 'xml'}"


def build(offline: bool, snapshot: str) -> sqlite3.Connection:
    meta_path = RAW_DIR / "meta.json"
    metas = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
    if not offline:
        for name, url in SOURCES.items():
            print(f"downloading {name} ...", flush=True)
            metas[name] = download(name, url, raw_path(name))
            meta_path.write_text(json.dumps(metas, indent=1), encoding="utf-8")
        # EU Annex XLII: find the newest consolidated text, then fetch it
        print("downloading eu_833_index ...", flush=True)
        metas["eu_833_index"] = download("eu_833_index", EU_833_INDEX, raw_path("eu_833_index"),
                                         check=check_html_complete, use_gzip=True)
        ver = eu_833_versions(raw_path("eu_833_index").read_text(encoding="utf-8", errors="replace"))
        url = EU_833_CONSOLIDATED.format(celex=ver["celex"])
        print(f"downloading eu_vessels ({ver['celex']}) ...", flush=True)
        metas["eu_vessels"] = {**download("eu_vessels", url, raw_path("eu_vessels"), check=check_html_complete,
                                        use_gzip=True),
                               **ver}
        meta_path.write_text(json.dumps(metas, indent=1), encoding="utf-8")
    missing = [n for n in (*SOURCES, *HTML_SOURCES) if not raw_path(n).exists() or n not in metas]
    if missing:
        raise SystemExit(f"sanctions: no cached download for {missing}; run without --offline first")
    if offline:   # the snapshot is dated by the download, not by today
        snapshot = min(m["fetched_at"][:10] for m in metas.values())
        # re-derive the newest amending act from the cached EUR-Lex page, so the
        # lag warning works offline too (and repairs a meta.json written before
        # that field was parsed). The version actually read stays as downloaded.
        ver = eu_833_versions(raw_path("eu_833_index").read_text(encoding="utf-8", errors="replace"))
        ev = metas["eu_vessels"]
        if (ev.get("latest_amendment_date"), ev.get("latest_amendment_celex")) != \
                (ver["latest_amendment_date"], ver["latest_amendment_celex"]):
            ev.update(latest_amendment_date=ver["latest_amendment_date"],
                      latest_amendment_celex=ver["latest_amendment_celex"])
            meta_path.write_text(json.dumps(metas, indent=1), encoding="utf-8")

    con = connect()
    dates = parse_ofac_listing_dates(raw_path("ofac_sdn_adv"))
    parsed = {
        "ofac_sdn": parse_ofac(raw_path("ofac_sdn"), "ofac_sdn", dates),
        "ofac_cons": parse_ofac(raw_path("ofac_cons"), "ofac_cons"),
        "uk": parse_uk(raw_path("uk")),
        "eu_fsf": parse_eu(raw_path("eu_fsf")),
        "eu_vessels": parse_eu_vessels(raw_path("eu_vessels"), metas["eu_vessels"]),
    }
    warning = eu_lag_warning(metas["eu_vessels"])
    if warning:
        print(warning, file=sys.stderr)
    print(f"\nsnapshot {snapshot} -> {DB_PATH.relative_to(ROOT)}")
    print(f"{'list':10} {'published':10} {'vessels':>7} {'valid IMO':>9} {'bad IMO':>7} {'no IMO':>6} {'dated':>6}")
    for lid, (entries, pmeta) in parsed.items():
        meta = {**metas[lid], **pmeta}
        row = store(con, snapshot, lid, entries, meta)
        ves = [e for e in entries if e.kind == "vessel"]
        bad = sum(1 for e in ves if e.imo_raw and not e.imo)
        none = sum(1 for e in ves if not e.imo_raw)
        dated = len({e.entry_id for e in ves if e.listed_on})
        print(f"{lid:10} {meta.get('publish_date') or '?':10} {row['n_vessels']:>7} {row['n_valid_imo']:>9} "
              f"{bad:>7} {none:>6} {dated:>6}")
        comp = sum(1 for e in entries if e.kind == "company")
        if comp:
            print(f"{'':10} + {comp} company IMO (owner) numbers, kept but not matchable against AIS")
    for e in (e for es, _ in parsed.values() for e in es if e.imo_raw and not e.imo and e.kind == "vessel"):
        print(f"  invalid IMO kept unmatched: {e.list_id} entry {e.entry_id}: {e.imo_raw!r}")
    union = {r[0] for r in con.execute(
        "SELECT DISTINCT imo FROM vessels WHERE snapshot=? AND kind='vessel' AND imo IS NOT NULL", (snapshot,))}
    print(f"unique valid vessel IMOs across all lists: {len(union)}")
    build.snapshot = snapshot
    return con


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--offline", action="store_true", help="re-parse cached downloads only")
    ap.add_argument("--no-match", action="store_true", help="skip the AIS match")
    ap.add_argument("--ais-db", type=Path, default=AIS_DB)
    a = ap.parse_args(argv)
    try:
        con = build(a.offline, dt.datetime.now(dt.timezone.utc).date().isoformat())
        if a.no_match:
            return 0
        res = match(con, build.snapshot, *read_ais(a.ais_db))
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001
        print(f"sanctions: FAILED: {e}", file=sys.stderr)
        return 1
    print(f"\nAIS match (PRIVATE, do not publish): {res['ais_ships_with_imo']} ships with an IMO in ship_static, "
          f"{res['ais_valid_imo']} pass the check digit ({res['ais_unique_valid_imo']} unique)")
    by_list: dict[str, int] = {}
    for _, _, lists in res["matches"]:
        for lid in lists:
            by_list[lid] = by_list.get(lid, 0) + 1
    print(f"matched: {len(res['matches'])}" + (f"  ({', '.join(f'{k} {v}' for k, v in sorted(by_list.items()))})"
                                                if by_list else ""))
    for mmsi, imo, lists in sorted(res["matches"], key=lambda x: x[1]):
        print(f"  IMO {imo}  MMSI {mmsi}  {', '.join(lists)}")
    print(f"MMSI-only leads (MMSIs get reassigned: check before use): {len(res['mmsi_matches'])} "
          f"of {res['ais_mmsis']} MMSIs seen, against {res['listed_mmsis']} listed MMSIs")
    for mmsi, lists in res["mmsi_matches"]:
        print(f"  MMSI {mmsi}  {', '.join(lists)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
