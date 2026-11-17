"""Shared logic for the AIS recorder and the Phase 0 coverage test (ticket #6).

Pure functions + SQLite helpers, no network: everything here is unit-tested
against tests/fixtures/ais_sample.jsonl.

Responsible design: the store keeps MMSI / IMO / destination for later
ship-level work, but never the ship name, and every output is an aggregate
count. Nothing from here is published.
"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "var" / "ais" / "ais.sqlite"   # gitignored (var/, *.sqlite)

# aisstream.io bounding boxes: [[lat_min, lon_min], [lat_max, lon_max]]
BOXES = {
    # Strait + Gulf of Oman anchorages (Fujairah / Khor Fakkan) + inner approach
    "hormuz": [[24.0, 54.8], [27.6, 58.8]],
    # Bab el-Mandeb, with the southern Red Sea and Gulf of Aden approaches
    "bab_el_mandeb": [[11.0, 42.0], [14.0, 45.5]],
}

# Gate lines (lat, lon) -> (lat, lon), coast to coast across the narrows.
# Approximate, hand-placed; a crossing anywhere along the line counts.
GATES = {
    # Musandam tip (Oman) north to the Iranian coast west of Larak island:
    # every ship entering or leaving the Gulf by sea must cross it.
    "hormuz": ((26.38, 56.42), (27.05, 56.35)),
    # Ras Siyyan (Djibouti) to the Yemeni shore, across both the main
    # channel west of Perim island and the small strait east of it.
    "bab_el_mandeb": ((12.48, 43.32), (12.70, 43.50)),
}

MAX_GAP_HOURS = 6.0        # don't join two positions further apart than this
DEDUP_HOURS = 2.0          # same ship re-crossing within this window = 1 crossing

PW_TYPES = ("container", "dry_bulk", "general_cargo", "roro", "tanker")


def map_ship_type(ais_type: int | None) -> str:
    """AIS ship-type code -> PortWatch-comparable class.

    AIS cannot tell container / dry bulk / general cargo / ro-ro apart (all are
    70-79 'cargo'), so those four PortWatch types are compared as one 'cargo'
    group. 'unknown' = no static message seen yet; 'other' = not counted by
    PortWatch (tugs, fishing, passenger, military, ...)."""
    if ais_type is None or ais_type == 0:
        return "unknown"
    if 80 <= ais_type <= 89:
        return "tanker"
    if 70 <= ais_type <= 79:
        return "cargo"
    return "other"


# ----------------------------------------------------------------- parsing
def parse_time(s: str) -> str:
    """'2026-11-17 20:40:52.123456789 +0000 UTC' -> '2026-11-17T20:40:52.123456Z'."""
    s = s.strip()
    if "T" in s and s.endswith("Z"):
        return s
    date_part, time_part = s.split(" ")[:2]
    if "." in time_part:
        hms, frac = time_part.split(".")
        time_part = f"{hms}.{(frac + '000000')[:6]}"
    else:
        time_part += ".000000"
    return f"{date_part}T{time_part}Z"


@dataclass
class Position:
    mmsi: int
    ts: str
    lat: float
    lon: float
    sog: float | None
    cog: float | None
    nav_status: int | None
    msg_type: str


@dataclass
class Static:
    mmsi: int
    ts: str
    ais_type: int | None
    imo: int | None
    destination: str | None
    length_m: float | None
    draught_m: float | None


def parse_message(raw: str | dict) -> Position | Static | None:
    """One aisstream.io JSON message -> Position / Static, or None if irrelevant."""
    m = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
    kind = m.get("MessageType")
    meta = m.get("MetaData") or {}
    body = (m.get("Message") or {}).get(kind) or {}
    mmsi = meta.get("MMSI") or body.get("UserID")
    if not mmsi or "time_utc" not in meta:
        return None
    ts = parse_time(meta["time_utc"])
    if kind in ("PositionReport", "StandardClassBPositionReport", "ExtendedClassBPositionReport"):
        lat, lon = body.get("Latitude", meta.get("latitude")), body.get("Longitude", meta.get("longitude"))
        if lat is None or lon is None or abs(lat) > 90 or abs(lon) > 180:
            return None  # 91/181 = "not available" in AIS
        return Position(int(mmsi), ts, float(lat), float(lon), body.get("Sog"), body.get("Cog"),
                        body.get("NavigationalStatus"), kind)
    if kind == "ShipStaticData":
        dim = body.get("Dimension") or {}
        length = (dim.get("A") or 0) + (dim.get("B") or 0)
        return Static(int(mmsi), ts, body.get("Type"), body.get("ImoNumber") or None,
                      (body.get("Destination") or "").strip() or None,
                      float(length) or None, body.get("MaximumStaticDraught"))
    return None


# ----------------------------------------------------------------- storage
SCHEMA = """
CREATE TABLE IF NOT EXISTS positions (
  mmsi INTEGER NOT NULL, ts TEXT NOT NULL, lat REAL NOT NULL, lon REAL NOT NULL,
  sog REAL, cog REAL, nav_status INTEGER, msg_type TEXT,
  PRIMARY KEY (mmsi, ts)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS ship_static (
  mmsi INTEGER PRIMARY KEY, ais_type INTEGER, imo INTEGER, destination TEXT,
  length_m REAL, draught_m REAL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT NOT NULL, last_seen_at TEXT NOT NULL
);
"""


def connect(path: Path = DB_PATH) -> sqlite3.Connection:
    path = Path(path)
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path))
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.executescript(SCHEMA)
    return con


def store(con: sqlite3.Connection, items) -> tuple[int, int]:
    """Insert parsed items; duplicates (same mmsi + timestamp) are ignored, so
    replays and reconnect overlaps never create duplicate rows."""
    pos = [(p.mmsi, p.ts, p.lat, p.lon, p.sog, p.cog, p.nav_status, p.msg_type)
           for p in items if isinstance(p, Position)]
    sta = [(s.mmsi, s.ais_type, s.imo, s.destination, s.length_m, s.draught_m, s.ts)
           for s in items if isinstance(s, Static)]
    before = con.total_changes
    con.executemany("INSERT OR IGNORE INTO positions VALUES (?,?,?,?,?,?,?,?)", pos)
    n_pos = con.total_changes - before
    # keep the newest static record per ship
    con.executemany(
        """INSERT INTO ship_static VALUES (?,?,?,?,?,?,?)
           ON CONFLICT(mmsi) DO UPDATE SET ais_type=COALESCE(excluded.ais_type, ais_type),
             imo=COALESCE(excluded.imo, imo), destination=excluded.destination,
             length_m=COALESCE(excluded.length_m, length_m), draught_m=COALESCE(excluded.draught_m, draught_m),
             updated_at=excluded.updated_at
           WHERE excluded.updated_at >= ship_static.updated_at""", sta)
    con.commit()
    return n_pos, len(sta)


# ----------------------------------------------------------------- geometry
def _ts(s: str) -> dt.datetime:
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


def segment_crossing(p1, p2, gate) -> float | None:
    """Fraction t along p1->p2 where it crosses the gate segment, else None.
    Points are (lat, lon); a flat-plane approximation is fine at these scales."""
    (ay, ax), (by, bx) = gate
    (py, px), (qy, qx) = p1, p2
    rx, ry = qx - px, qy - py
    sx, sy = bx - ax, by - ay
    den = rx * sy - ry * sx
    if den == 0:
        return None
    t = ((ax - px) * sy - (ay - py) * sx) / den
    u = ((ax - px) * ry - (ay - py) * rx) / den
    if 0 <= t <= 1 and 0 <= u <= 1:
        return t
    return None


def gate_crossings(con: sqlite3.Connection, strait: str, since: str | None = None, until: str | None = None):
    """List of (mmsi, crossing_time_iso, direction, pw_class) for one strait."""
    gate = GATES[strait]
    (la0, lo0), (la1, lo1) = BOXES[strait]
    q = ("SELECT mmsi, ts, lat, lon FROM positions WHERE lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?"
         + (" AND ts >= ?" if since else "") + (" AND ts < ?" if until else "") + " ORDER BY mmsi, ts")
    args = [la0, la1, lo0, lo1] + ([since] if since else []) + ([until] if until else [])
    types = dict(con.execute("SELECT mmsi, ais_type FROM ship_static").fetchall())
    out, prev, last_cross = [], None, {}
    gx, gy = gate[1][1] - gate[0][1], gate[1][0] - gate[0][0]
    for mmsi, ts, lat, lon in con.execute(q, args):
        if prev and prev[0] == mmsi:
            t0, t1 = _ts(prev[1]), _ts(ts)
            gap_h = (t1 - t0).total_seconds() / 3600
            if 0 < gap_h <= MAX_GAP_HOURS:
                f = segment_crossing((prev[2], prev[3]), (lat, lon), gate)
                if f is not None:
                    when = t0 + (t1 - t0) * f
                    lc = last_cross.get(mmsi)
                    if lc is None or (when - lc).total_seconds() / 3600 > DEDUP_HOURS:
                        side = gx * (lat - prev[2]) - gy * (lon - prev[3])
                        out.append((mmsi, when.isoformat().replace("+00:00", "Z"),
                                    "a_to_b" if side > 0 else "b_to_a", map_ship_type(types.get(mmsi))))
                    last_cross[mmsi] = when
        prev = (mmsi, ts, lat, lon)
    return out


def daily_counts(crossings) -> dict[str, dict[str, int]]:
    """{date: {tanker, cargo, unknown, other, total_pw_comparable}}"""
    days: dict[str, dict[str, int]] = {}
    for _, when, _, cls in crossings:
        d = days.setdefault(when[:10], {"tanker": 0, "cargo": 0, "unknown": 0, "other": 0})
        d[cls] += 1
    for d in days.values():
        d["total_pw_comparable"] = d["tanker"] + d["cargo"]
    return dict(sorted(days.items()))


def uptime_hours_by_day(con: sqlite3.Connection) -> dict[str, float]:
    """Hours per UTC day the recorder was connected (from the sessions table)."""
    hours: dict[str, float] = {}
    for start, end in con.execute("SELECT started_at, last_seen_at FROM sessions"):
        a, b = _ts(start), _ts(end)
        while a < b:
            day_end = dt.datetime.combine(a.date() + dt.timedelta(days=1), dt.time(), tzinfo=a.tzinfo)
            seg_end = min(b, day_end)
            k = a.date().isoformat()
            hours[k] = hours.get(k, 0.0) + (seg_end - a).total_seconds() / 3600
            a = seg_end
    return {k: round(min(v, 24.0), 2) for k, v in sorted(hours.items())}


def load_portwatch(path: Path) -> dict[str, dict]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    rows = raw if isinstance(raw, list) else next(raw[k] for k in ("daily", "rows", "data") if isinstance(raw.get(k), list))
    return {r["date"][:10]: r for r in rows}


def coverage_report(con, strait: str, portwatch: dict[str, dict], min_uptime_h: float = 23.0,
                    flag_below: float = 0.5) -> dict:
    counts = daily_counts(gate_crossings(con, strait))
    up = uptime_hours_by_day(con)
    days = []
    for date in sorted(set(up) | set(counts)):
        c = counts.get(date, {"tanker": 0, "cargo": 0, "unknown": 0, "other": 0, "total_pw_comparable": 0})
        pw = portwatch.get(date)
        row = {"date": date, "uptime_h": up.get(date, 0.0), "ais": c}
        if up.get(date, 0.0) < min_uptime_h:
            row["status"] = "partial_day"          # not a full day of recording: not scored
        elif pw is None or pw.get("total") in (None, 0):
            row["status"] = "awaiting_portwatch"   # PortWatch settles ~2 days later
        else:
            bt = pw.get("by_type") or {}
            pw_tanker = bt.get("tanker") or 0
            pw_cargo = (pw["total"] or 0) - pw_tanker
            row.update({
                "status": "scored",
                "portwatch": {"total": pw["total"], "tanker": pw_tanker, "cargo": pw_cargo},
                "coverage_total": round(c["total_pw_comparable"] / pw["total"], 3),
                "coverage_total_incl_unknown": round((c["total_pw_comparable"] + c["unknown"]) / pw["total"], 3),
                "coverage_tanker": round(c["tanker"] / pw_tanker, 3) if pw_tanker else None,
                "coverage_cargo": round(c["cargo"] / pw_cargo, 3) if pw_cargo else None,
            })
        days.append(row)
    scored = [d for d in days if d["status"] == "scored"]
    summary = None
    if scored:
        ais = sum(d["ais"]["total_pw_comparable"] for d in scored)
        ais_u = ais + sum(d["ais"]["unknown"] for d in scored)
        pw = sum(d["portwatch"]["total"] for d in scored)
        cov = ais / pw
        summary = {"days_scored": len(scored), "ais_crossings": ais, "portwatch_transits": pw,
                   "coverage": round(cov, 3), "coverage_incl_unknown": round(ais_u / pw, 3),
                   "below_gate": cov < flag_below,
                   "verdict": (f"Free AIS saw {cov:.0%} of PortWatch transits "
                               f"({'UNDER' if cov < flag_below else 'at or above'} the {flag_below:.0%} gate).")}
    return {"strait": strait, "gate": GATES[strait], "days": days, "summary": summary}
