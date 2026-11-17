#!/usr/bin/env python3
"""Layer A: 7-day strait transit forecast + daily regime label (ticket #3).

Reads   data/transits/<strait>.json   (written by the PortWatch pipeline, #2)
        data/ledger.json              (optional, hand-curated incidents)
Writes  data/forecast/<strait>.json   (contract in ticket #1)

Method (deliberately simple and explainable):
  * Level  = exponentially weighted mean of recent daily transits. The half-life
    is chosen on a validation window that ends BEFORE the backtest window, so
    the backtest never sees data used to tune the model.
  * Range  = level + empirical 10th/90th percentile of the model's own recent
    forecast errors (last 56 days of past issue dates whose outcome is known).
  * Naive  = mean of the last 7 days (and "same as yesterday"), same interval rule.
  * Backtest: rolling origin, strictly time-ordered, last 26 weeks, horizons 1-7.
    If the model's MAE does not beat the best naive baseline, the naive forecast
    is published instead and the file says so.
  * Regime = last-7-day mean vs a dated pre-crisis baseline, plus the 7-day count
    of independently reported incidents in data/ledger.json.

Usage:  python scripts/forecast.py [--data-dir data] [--strait hormuz ...]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd

STRAITS = ("hormuz", "bab_el_mandeb")
SHIP_TYPES = ("container", "dry_bulk", "general_cargo", "roro", "tanker")
HORIZON = 7

# Pre-crisis baselines (dated so every number on the page can be traced).
BASELINES = {
    "hormuz": {
        "start": "2025-01-01", "end": "2025-12-31",
        "label": "2025 average (before the 2026 conflict)",
    },
    "bab_el_mandeb": {
        "start": "2023-01-01", "end": "2023-10-31",
        "label": "Jan-Oct 2023 average (before the Red Sea crisis)",
    },
}
STRAIT_NAMES = {"hormuz": "Strait of Hormuz", "bab_el_mandeb": "Bab el-Mandeb"}

# Regime thresholds on (last-7-day mean / pre-crisis mean).
OPEN_RATIO = 0.70     # at or above: open
CLOSED_RATIO = 0.10   # below: closed (normal commercial traffic has stopped)

HALF_LIVES = (2, 3, 5, 7, 10, 14, 21, 28)   # days, candidates for the EWMA level
ERROR_WINDOW = 56                          # days of past errors used for p10/p90
BACKTEST_DAYS = 182                        # ~26 weeks, the most recent ones
VALIDATION_DAYS = 91                       # ~13 weeks just before the backtest
Q_LO, Q_HI = 0.10, 0.90


# --------------------------------------------------------------------------- io
def load_transits(path: Path) -> tuple[pd.DataFrame, dict]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, list):
        rows, meta = raw, {}
    else:
        rows = next((raw[k] for k in ("daily", "rows", "data", "days") if isinstance(raw.get(k), list)), None)
        if rows is None:
            raise ValueError(f"{path}: no list of daily rows found (expected key 'daily'/'rows'/'data')")
        meta = {k: v for k, v in raw.items() if not isinstance(v, list)}
    recs = []
    for r in rows:
        bt = r.get("by_type") or {}
        rec = {"date": r["date"], "total": r.get("total")}
        for t in SHIP_TYPES:
            rec[t] = bt.get(t)
        recs.append(rec)
    df = pd.DataFrame(recs)
    df["date"] = pd.to_datetime(df["date"]).dt.tz_localize(None).dt.normalize()
    df = df.drop_duplicates("date", keep="last").set_index("date").sort_index()
    df = df.apply(pd.to_numeric, errors="coerce")
    # Reindex to a continuous calendar; short gaps are filled by interpolation so
    # the rolling backtest stays aligned. Long gaps stay NaN and are skipped.
    df = df.asfreq("D").interpolate(limit=3, limit_area="inside")
    return df, meta


def load_incidents(ledger_path: Path, strait: str, end: pd.Timestamp) -> tuple[int | None, int]:
    """(independently reported incidents in the 7 days to `end`, claims-only count).
    None when no usable ledger exists. Example/placeholder rows are ignored."""
    if not ledger_path.exists():
        return None, 0
    try:
        raw = json.loads(ledger_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, 0
    rows = raw if isinstance(raw, list) else next(
        (raw[k] for k in ("rows", "incidents", "entries", "data") if isinstance(raw.get(k), list)), [])
    confirmed = claimed = 0
    start = end - pd.Timedelta(days=6)
    for r in rows:
        if not isinstance(r, dict) or r.get("example") or r.get("is_example"):
            continue
        if str(r.get("id", "")).lower().startswith("example"):
            continue
        rs = str(r.get("strait", "")).lower().replace("-", "_").replace(" ", "_")
        if rs and strait not in rs and rs not in strait:
            continue
        try:
            d = pd.Timestamp(r.get("date")).tz_localize(None).normalize()
        except (ValueError, TypeError):
            continue
        if not (start <= d <= end):
            continue
        status = str(r.get("status", "")).lower()
        # claimed_only, or no independent source named -> a claim, not counted
        if ("claim" in status and "confirm" not in status) or (
                "independent_source" in r and not r.get("independent_source")):
            claimed += 1
        else:
            confirmed += 1
    return confirmed, claimed


# ----------------------------------------------------------------------- models
def ewma_levels(y: pd.Series, half_life: float) -> pd.Series:
    """Level known at the END of each day (uses data up to and including that day)."""
    return y.ewm(halflife=half_life, adjust=True, ignore_na=True).mean()


def ma7_levels(y: pd.Series) -> pd.Series:
    return y.rolling(7, min_periods=4).mean()


def last_levels(y: pd.Series) -> pd.Series:
    return y.ffill(limit=2)


def future_matrix(y: pd.Series) -> np.ndarray:
    """F[i, h-1] = y[i + h] (NaN past the end)."""
    v = y.to_numpy(dtype=float)
    n = len(v)
    out = np.full((n, HORIZON), np.nan)
    for h in range(1, HORIZON + 1):
        out[: n - h, h - 1] = v[h:]
    return out


def interval_offsets(err: np.ndarray, err_week: np.ndarray, i: int) -> tuple[float, float, float, float]:
    """10th/90th percentile of daily and weekly-sum errors from issue dates whose
    whole 7-day outcome was known at issue index i (no look-ahead)."""
    hi_idx = i - HORIZON          # outcome of issue j fully known at j + 7 <= i
    lo_idx = max(0, hi_idx - ERROR_WINDOW + 1)
    if hi_idx < lo_idx:
        return (np.nan,) * 4
    d = err[lo_idx: hi_idx + 1].ravel()
    d = d[~np.isnan(d)]
    w = err_week[lo_idx: hi_idx + 1]
    w = w[~np.isnan(w)]
    if len(d) < 30 or len(w) < 10:
        return (np.nan,) * 4
    return (float(np.quantile(d, Q_LO)), float(np.quantile(d, Q_HI)),
            float(np.quantile(w, Q_LO)), float(np.quantile(w, Q_HI)))


class Forecaster:
    """A level function + empirical error intervals, evaluated without look-ahead."""

    def __init__(self, name: str, levels: pd.Series, y: pd.Series):
        self.name = name
        self.y = y
        self.lev = levels.to_numpy(dtype=float)
        self.fut = future_matrix(y)
        self.err = self.fut - self.lev[:, None]
        self.err_week = self.fut.sum(axis=1) - HORIZON * self.lev  # NaN if any day missing

    def predict(self, i: int) -> dict | None:
        lv = self.lev[i]
        if np.isnan(lv):
            return None
        dlo, dhi, wlo, whi = interval_offsets(self.err, self.err_week, i)
        if np.isnan(dlo):
            return None
        return {
            "p50": max(lv, 0.0),
            "p10": max(lv + dlo, 0.0), "p90": max(lv + dhi, 0.0),
            "w50": max(HORIZON * lv, 0.0),
            "w10": max(HORIZON * lv + wlo, 0.0), "w90": max(HORIZON * lv + whi, 0.0),
        }


def evaluate(fc: Forecaster, idx: range) -> dict:
    """MAE of p50 over all (issue date, horizon) pairs in idx, plus 80% coverage."""
    abs_err, inside, n = 0.0, 0, 0
    for i in idx:
        p = fc.predict(i)
        if p is None:
            continue
        actual = fc.fut[i]
        ok = ~np.isnan(actual)
        if not ok.any():
            continue
        a = actual[ok]
        abs_err += float(np.abs(a - p["p50"]).sum())
        inside += int(((a >= p["p10"]) & (a <= p["p90"])).sum())
        n += int(ok.sum())
    if n == 0:
        return {"mae": None, "coverage_80": None, "n": 0}
    return {"mae": abs_err / n, "coverage_80": inside / n, "n": n}


def build_forecasters(y: pd.Series, half_life: float) -> dict[str, Forecaster]:
    return {
        "model": Forecaster(f"ewma_hl{half_life:g}", ewma_levels(y, half_life), y),
        "naive_ma7": Forecaster("naive_ma7", ma7_levels(y), y),
        "naive_last": Forecaster("naive_last", last_levels(y), y),
    }


def choose_half_life(y: pd.Series, val_idx: range) -> float:
    best, best_mae = HALF_LIVES[0], math.inf
    for hl in HALF_LIVES:
        r = evaluate(Forecaster("x", ewma_levels(y, hl), y), val_idx)
        if r["mae"] is not None and r["mae"] < best_mae:
            best, best_mae = hl, r["mae"]
    return best


# ---------------------------------------------------------------------- regime
def regime_for(y: pd.Series, i: int, baseline: float, incidents: int | None, claims: int,
               name: str, base_label: str) -> tuple[str, list[str]]:
    win = y.iloc[max(0, i - 6): i + 1].dropna()
    prior = y.iloc[max(0, i - 34): max(0, i - 6)].dropna()  # the 28 days before that
    ma7 = float(win.mean()) if len(win) else float("nan")
    ratio = ma7 / baseline if baseline > 0 else float("nan")
    asof = y.index[i].date().isoformat()
    reasons = [
        f"{name}: {ma7:.1f} transits/day over the 7 days to {asof}, "
        f"{ratio * 100:.0f}% of the {base_label} ({baseline:.1f}/day)."
    ]
    if ratio >= OPEN_RATIO:
        label = "open"
    elif ratio >= CLOSED_RATIO:
        label = "contested"
        reasons.append(f"Traffic is between {CLOSED_RATIO:.0%} and {OPEN_RATIO:.0%} of normal, "
                       "so ships are passing but many are staying away or rerouting.")
    else:
        label = "closed"
        reasons.append(f"Traffic is under {CLOSED_RATIO:.0%} of normal: only a handful of ships "
                       "a day pass, so the strait is effectively closed to normal commercial traffic.")
    if len(prior) >= 14 and np.mean(prior) > 0:
        chg = ma7 / float(np.mean(prior)) - 1
        if abs(chg) >= 0.15:
            reasons.append(f"Trend: the last 7 days are {abs(chg):.0%} {'above' if chg > 0 else 'below'} "
                           "the 28 days before.")
        else:
            reasons.append("Trend: flat versus the 28 days before (within 15%).")
    if incidents is None:
        reasons.append("Incident ledger not available, so incidents are not counted in this label.")
    else:
        reasons.append(f"{incidents} independently reported incident(s) in the 7 days to {asof}"
                       + (f"; {claims} further claim(s) not independently confirmed and not counted." if claims else "."))
        if incidents >= 1 and label == "open":
            label = "contested"
            reasons.append("Raised from open to contested because of recent independently reported incidents.")
    return label, reasons


# ------------------------------------------------------------------------ main
def r1(x: float) -> float:
    return round(float(x), 1)


def forecast_series(y: pd.Series, test_idx: range, val_idx: range, issue_i: int) -> dict:
    hl = choose_half_life(y, val_idx)
    fcs = build_forecasters(y, hl)
    scores = {k: evaluate(f, test_idx) for k, f in fcs.items()}
    naive_key = min(("naive_ma7", "naive_last"),
                    key=lambda k: scores[k]["mae"] if scores[k]["mae"] is not None else math.inf)
    m, nv = scores["model"]["mae"], scores[naive_key]["mae"]
    beats = m is not None and nv is not None and m < nv
    used = "model" if beats else naive_key
    pred = fcs[used].predict(issue_i)
    if pred is None:  # not enough history for intervals: fall back to naive point, flat range
        lv = float(ma7_levels(y).iloc[issue_i])
        pred = {"p50": lv, "p10": lv, "p90": lv, "w50": 7 * lv, "w10": 7 * lv, "w90": 7 * lv}
    return {"pred": pred, "half_life": hl, "scores": scores, "naive_key": naive_key,
            "beats": beats, "used": used, "used_name": fcs[used].name}


def run_strait(strait: str, data_dir: Path, now: dt.datetime) -> dict:
    df, meta = load_transits(data_dir / "transits" / f"{strait}.json")
    df = df.loc[: df["total"].last_valid_index()]
    y = df["total"]
    n = len(y)
    issue_i = n - 1
    last_date = y.index[issue_i]

    # Time-ordered windows: [validation][backtest] ending at the last issue date
    # whose 7-day outcome is fully known.
    last_eval = n - 1 - HORIZON
    test_start = max(0, last_eval - BACKTEST_DAYS + 1)
    val_start = max(0, test_start - VALIDATION_DAYS)
    test_idx = range(test_start, last_eval + 1)
    val_idx = range(val_start, test_start)

    total = forecast_series(y, test_idx, val_idx, issue_i)
    p = total["pred"]
    dates = [(last_date + pd.Timedelta(days=h)).date().isoformat() for h in range(1, HORIZON + 1)]

    by_type = {}
    for t in SHIP_TYPES:
        s = df[t]
        if s.notna().sum() < VALIDATION_DAYS + BACKTEST_DAYS // 2:
            continue
        ft = forecast_series(s, test_idx, val_idx, issue_i)
        q = ft["pred"]
        sc = ft["scores"]
        # p10/p50/p90 at the top level are 7-day totals (data/README.md contract);
        # daily quantiles and per-type backtest are extra detail.
        by_type[t] = {
            "p10": r1(q["w10"]), "p50": r1(q["w50"]), "p90": r1(q["w90"]),
            "daily": [{"date": d, "p10": r1(q["p10"]), "p50": r1(q["p50"]), "p90": r1(q["p90"])} for d in dates],
            "method": "model" if ft["beats"] else "naive_baseline",
            "backtest": {
                "mae_model": _rd(sc["model"]["mae"]), "mae_naive": _rd(sc[ft["naive_key"]]["mae"]),
                "coverage_80": _rd(sc[ft["used"]]["coverage_80"]),
            },
        }

    # Regime: today's label plus the last 30 days, each computed only from data to that day.
    baseline_cfg = BASELINES[strait]
    base_val = float(y[baseline_cfg["start"]: baseline_cfg["end"]].mean())
    ledger = data_dir / "ledger.json"
    history = []
    for i in range(max(0, n - 30), n):
        inc, cl = load_incidents(ledger, strait, y.index[i])
        lab, rs = regime_for(y, i, base_val, inc, cl, STRAIT_NAMES[strait], baseline_cfg["label"])
        history.append({"date": y.index[i].date().isoformat(), "regime": lab})
    inc, cl = load_incidents(ledger, strait, last_date)
    regime, reasons = regime_for(y, issue_i, base_val, inc, cl, STRAIT_NAMES[strait], baseline_cfg["label"])

    sc = total["scores"]
    nk = total["naive_key"]
    used = total["used"]
    test_from = y.index[test_start].date().isoformat()
    test_to = y.index[last_eval].date().isoformat()
    if total["beats"]:
        verdict = (f"Model beats the naive baseline ({nk.replace('naive_', 'naive ')}) in the backtest "
                   f"(MAE {sc['model']['mae']:.2f} vs {sc[nk]['mae']:.2f}); the model forecast is shown.")
    else:
        verdict = (f"Model does NOT beat the naive baseline ({nk.replace('naive_', 'naive ')}) in the backtest "
                   f"(MAE {sc['model']['mae']:.2f} vs {sc[nk]['mae']:.2f}); the naive baseline is shown instead.")

    if total["beats"]:
        method_note = (f"Forecast = recent daily transits averaged with weights that halve every "
                       f"{total['half_life']:g} days going back, so the most recent days count most. Range = 10th-90th percentile "
                       f"of this method's own errors over the past 8 weeks. Tested on {test_from} to {test_to}, "
                       f"never using future data; it beat the naive baseline.")
    else:
        what = "the last 7-day average" if nk == "naive_ma7" else "yesterday's count"
        method_note = (f"Forecast = {what}, repeated for 7 days: our weighted model did not beat this naive "
                       f"baseline in a time-ordered test ({test_from} to {test_to}), so we show the baseline. "
                       f"Range = 10th-90th percentile of its own errors over the past 8 weeks.")

    return {
        "strait": strait,
        "issued_at": now.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "last_data_date": last_date.date().isoformat(),
        "source": meta.get("source", "IMF PortWatch Daily Chokepoints"),
        "regime": regime,
        "regime_reasons": reasons,
        "regime_history": history,
        "horizon_days": HORIZON,
        "daily": [{"date": d, "p10": r1(p["p10"]), "p50": r1(p["p50"]), "p90": r1(p["p90"])} for d in dates],
        "weekly_total": {"p10": r1(p["w10"]), "p50": r1(p["w50"]), "p90": r1(p["w90"])},
        "by_type": by_type,
        "method": "model" if total["beats"] else "naive_baseline",
        "backtest": {
            "mae_model": _rd(sc["model"]["mae"]),
            "mae_naive": _rd(sc[nk]["mae"]),
            "naive_kind": nk,
            "mae_naive_ma7": _rd(sc["naive_ma7"]["mae"]),
            "mae_naive_last": _rd(sc["naive_last"]["mae"]),
            "coverage_80": _rd(sc[used]["coverage_80"]),
            "coverage_80_model": _rd(sc["model"]["coverage_80"]),
            "coverage_80_naive": _rd(sc[nk]["coverage_80"]),
            "window": {"start": test_from, "end": test_to, "issue_dates": len(test_idx),
                       "forecast_points": sc["model"]["n"], "horizons": "1-7 days"},
            "model_beats_naive": total["beats"],
            "verdict": verdict,
            "model_spec": total["used_name"] if total["beats"] else f"{nk} (fallback); model was ewma_hl{total['half_life']:g}",
        },
        "method_note": method_note,
        "baseline": {"mean_per_day": r1(base_val), "period": f"{baseline_cfg['start']} to {baseline_cfg['end']}",
                     "label": baseline_cfg["label"]},
        "note": "Forecast dates start the day after the last settled PortWatch day. Not financial advice.",
    }


def _rd(x):
    return None if x is None or (isinstance(x, float) and math.isnan(x)) else round(float(x), 3)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--data-dir", default="data", type=Path)
    ap.add_argument("--strait", action="append", choices=STRAITS)
    args = ap.parse_args(argv)
    now = dt.datetime.now(dt.timezone.utc)
    out_dir = args.data_dir / "forecast"
    out_dir.mkdir(parents=True, exist_ok=True)
    failed = 0
    for strait in args.strait or STRAITS:
        try:
            res = run_strait(strait, args.data_dir, now)
        except Exception as e:  # keep the other strait; non-zero exit lets refresh.sh restore data/
            print(f"[forecast] {strait}: FAILED: {e}", file=sys.stderr)
            failed += 1
            continue
        words = len(res["method_note"].split())
        assert words < 60, f"method_note too long ({words} words)"
        tmp = out_dir / f".{strait}.json.tmp"
        tmp.write_text(json.dumps(res, indent=2), encoding="utf-8")
        tmp.replace(out_dir / f"{strait}.json")
        b = res["backtest"]
        print(f"[forecast] {strait}: regime={res['regime']} week p50={res['weekly_total']['p50']} "
              f"MAE model={b['mae_model']} naive({b['naive_kind']})={b['mae_naive']} "
              f"cov80={b['coverage_80']} -> {res['method']}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
