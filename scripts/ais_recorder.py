#!/usr/bin/env python3
"""AIS recorder for Hormuz + Bab el-Mandeb on the aisstream.io websocket (ticket #6).

    AISSTREAM_API_KEY=... python scripts/ais_recorder.py --hours 48
    python scripts/ais_recorder.py --replay tests/fixtures/ais_sample.jsonl   # offline

* The key is read from the AISSTREAM_API_KEY env var only. It is never written
  to disk or to the log, and any log line containing it is redacted.
* Restart-safe: reconnects with exponential backoff and jitter. Rows are keyed
  on (mmsi, timestamp), so overlaps after a reconnect or restart never
  duplicate. A sessions table records connected time, so the coverage test
  only scores full days.
* Stores into var/ais/ais.sqlite (gitignored). Nothing here is published.
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import logging
import os
import random
import signal
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ais_lib as L  # noqa: E402

WS_URL = "wss://stream.aisstream.io/v0/stream"
MESSAGE_TYPES = ["PositionReport", "StandardClassBPositionReport", "ExtendedClassBPositionReport",
                 "ShipStaticData"]
FLUSH_SECONDS, FLUSH_ROWS = 2.0, 500
STATS_SECONDS = 60
STABLE_SECONDS = 60          # a connection this old resets the backoff
SILENCE_TIMEOUT = 120        # no message for this long -> reconnect

log = logging.getLogger("ais")


class RedactFilter(logging.Filter):
    """Belt and braces: strip the API key from any log record."""

    def __init__(self, secret: str | None):
        super().__init__()
        self.secret = secret

    def filter(self, record: logging.LogRecord) -> bool:
        if self.secret:
            msg = record.getMessage()
            if self.secret in msg:
                record.msg, record.args = msg.replace(self.secret, "***"), ()
        return True


def setup_logging(secret: str | None) -> None:
    h = logging.StreamHandler(sys.stderr)
    h.setFormatter(logging.Formatter("%(asctime)sZ %(levelname)s %(message)s", "%Y-%m-%dT%H:%M:%S"))
    logging.Formatter.converter = time.gmtime
    h.addFilter(RedactFilter(secret))
    root = logging.getLogger()
    root.handlers[:] = [h]
    root.setLevel(logging.INFO)
    logging.getLogger("websockets").setLevel(logging.WARNING)


def backoff_delay(attempt: int, base: float = 1.0, cap: float = 60.0, rng=random.random) -> float:
    """Exponential backoff with full jitter: uniform(0.5, 1.0) * min(cap, base * 2**attempt)."""
    return min(cap, base * (2 ** attempt)) * (0.5 + 0.5 * rng())


def subscription(key: str) -> str:
    return json.dumps({"APIKey": key, "BoundingBoxes": list(L.BOXES.values()),
                       "FilterMessageTypes": MESSAGE_TYPES})


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class Recorder:
    def __init__(self, db_path: Path):
        self.con = L.connect(db_path)
        self.buf: list = []
        self.stats = {"msgs": 0, "pos_new": 0, "static": 0, "skipped": 0, "errors": 0}
        self.session_id: int | None = None

    def handle(self, raw) -> None:
        self.stats["msgs"] += 1
        try:
            item = L.parse_message(raw)
        except (ValueError, KeyError, TypeError):
            self.stats["errors"] += 1
            return
        if item is None:
            self.stats["skipped"] += 1
            return
        self.buf.append(item)
        if len(self.buf) >= FLUSH_ROWS:
            self.flush()

    def flush(self) -> None:
        if self.buf:
            n_pos, n_sta = L.store(self.con, self.buf)
            self.stats["pos_new"] += n_pos
            self.stats["static"] += n_sta
            self.buf.clear()
        if self.session_id is not None:
            self.con.execute("UPDATE sessions SET last_seen_at=? WHERE id=?", (now_iso(), self.session_id))
            self.con.commit()

    def open_session(self) -> None:
        t = now_iso()
        cur = self.con.execute("INSERT INTO sessions (started_at, last_seen_at) VALUES (?, ?)", (t, t))
        self.con.commit()
        self.session_id = cur.lastrowid

    def close_session(self) -> None:
        self.flush()
        self.session_id = None


async def run_live(rec: Recorder, key: str, deadline: float | None, stop: asyncio.Event) -> None:
    import websockets  # imported here so --replay and the tests need no extra package

    attempt = 0
    last_stats = time.monotonic()
    while not stop.is_set() and (deadline is None or time.monotonic() < deadline):
        started = time.monotonic()
        try:
            async with websockets.connect(WS_URL, open_timeout=20, ping_interval=30, ping_timeout=30,
                                          max_size=2 ** 22) as ws:
                await ws.send(subscription(key))   # must be sent within 3 s of connecting
                rec.open_session()
                log.info("connected; subscribed to %s", ", ".join(L.BOXES))
                last_flush = time.monotonic()
                while not stop.is_set() and (deadline is None or time.monotonic() < deadline):
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=SILENCE_TIMEOUT)
                    except asyncio.TimeoutError:
                        log.warning("no message for %ss, reconnecting", SILENCE_TIMEOUT)
                        break
                    if isinstance(raw, (bytes, bytearray)):
                        raw = raw.decode("utf-8", "replace")
                    if raw.startswith('{"error"'):
                        # aisstream reports a bad key / bad subscription this way
                        log.error("server error: %s", json.loads(raw).get("error", "unknown"))
                        break
                    rec.handle(raw)
                    t = time.monotonic()
                    if t - last_flush >= FLUSH_SECONDS:
                        rec.flush()
                        last_flush = t
                    if t - last_stats >= STATS_SECONDS:
                        log.info("stats %s", json.dumps(rec.stats))
                        last_stats = t
        except asyncio.CancelledError:
            raise
        except Exception as e:  # network errors, handshake failures, server closes
            rec.stats["errors"] += 1
            log.warning("connection error: %s", type(e).__name__)
        finally:
            rec.close_session()
        if stop.is_set() or (deadline is not None and time.monotonic() >= deadline):
            break
        attempt = 0 if time.monotonic() - started >= STABLE_SECONDS else attempt + 1
        delay = backoff_delay(attempt)
        log.info("reconnecting in %.1fs (attempt %d)", delay, attempt)
        try:
            await asyncio.wait_for(stop.wait(), timeout=delay)
        except asyncio.TimeoutError:
            pass


def replay(rec: Recorder, path: Path) -> None:
    """Feed a JSONL fixture through the same parse/store path (no session is
    recorded, so replayed days are reported as partial by the coverage test)."""
    lines = [ln for ln in Path(path).read_text(encoding="utf-8").splitlines() if ln.strip()]
    for ln in lines:
        rec.handle(ln)
    rec.flush()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Record aisstream.io AIS for Hormuz + Bab el-Mandeb.")
    ap.add_argument("--hours", type=float, default=None, help="stop after this many hours (default: run forever)")
    ap.add_argument("--db", type=Path, default=L.DB_PATH)
    ap.add_argument("--replay", type=Path, help="replay a JSONL fixture instead of connecting")
    args = ap.parse_args(argv)

    key = os.environ.get("AISSTREAM_API_KEY", "").strip()
    setup_logging(key or None)
    if args.replay:
        rec = Recorder(args.db)
        replay(rec, args.replay)
        log.info("replayed %s: %s", args.replay, json.dumps(rec.stats))
        return 0
    if not key:
        log.error("AISSTREAM_API_KEY is not set (free key: https://aisstream.io)")
        return 2
    rec = Recorder(args.db)

    stop = asyncio.Event()
    deadline = time.monotonic() + args.hours * 3600 if args.hours else None

    async def _main():
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop.set)
            except (NotImplementedError, RuntimeError):  # Windows: KeyboardInterrupt path below
                pass
        await run_live(rec, key, deadline, stop)

    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass
    finally:
        rec.close_session()
        log.info("stopped; totals %s", json.dumps(rec.stats))
    return 0


if __name__ == "__main__":
    sys.exit(main())
