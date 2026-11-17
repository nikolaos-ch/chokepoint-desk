#!/usr/bin/env python3
"""Layer A: 7-day strait transit forecast + daily regime label (ticket #3).

Reads   data/transits/<strait>.json   (written by the PortWatch pipeline, #2)
        data/ledger.json              (optional, hand-curated incidents)
Writes  data/forecast/<strait>.json   (contract in ticket #1, shapes in data/README.md)

Method (deliberately simple and explainable):
  * Model  = exponentially weighted mean of recent daily transits, repeated
    for the next 7 days.
  * Naive  = the better (on the selection window) of "mean of the last 7 days"
    and "same as yesterday".
  * Range  = level + empirical 10th/90th percentile of the method's own past
    errors (the 56 most recent issue dates whose 7-day outcome was known).
  * Choice rule: the model is shown only if the paired block-bootstrap 95% CI
    of its MAE gain over naive lies entirely above 0 ("clearly better");
    otherwise naive is shown and the file says "no meaningful difference".
  * Three 91-day, strictly time-ordered windows TUNE < SELECT < REPORT
    (rolling origin, horizons 1-7, never shuffled; each window's outcomes end
    before the next one starts):
      today's method: tune the half-life on SELECT, apply the rule on REPORT
        (the most recent data decides; user decision 17 Nov 2026)
      out-of-sample check: the same procedure one window earlier (tune on
        TUNE, choose on SELECT), scored on REPORT, which it never saw;
        published in backtest.out_of_sample_check
  * Regime = last-7-day mean vs a dated pre-crisis baseline, plus the 7-day
    count of independently reported incidents in data/ledger.json.

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
    "hormuz": {"start": "2025-01-01", "end": "2025-12-31",
               "label": "2025 average (before the 2026 conflict)"},
    "bab_el_mandeb": {"start": "2023-01-01", "end": "2023-10-31",
                      "label": "Jan-Oct 2023 average (before the Red Sea crisis)"},
}
STRAIT_NAMES = {"hormuz": "Strait of Hormuz", "bab_el_mandeb": "Bab el-Mandeb"}
STRAIT_ALIASES = {
    "hormuz": {"hormuz", "strait_of_hormuz"},
    "bab_el_mandeb": {"bab_el_mandeb", "bab_al_mandab", "bab_el_mandab", "bab_al_mandeb"},
}

# Regime thresholds on (last-7-day mean / pre-crisis mean).
OPEN_RATIO = 0.70     # at or above: open
CLOSED_RATIO = 0.10   # below: closed (normal commercial traffic has stopped)

HALF_LIVES = (2, 3, 5, 7, 10, 14, 21, 28)   # days, candidates for the EWMA level
ERROR_WINDOW = 56                          # issue dates of past errors used for p10/p90
TUNE_DAYS = SELECT_DAYS = REPORT_DAYS = 91  # ~13 weeks each, oldest -> newest
Q_LO, Q_HI = 0.10, 0.90
BOOT_N, BOOT_BLOCK, BOOT_SEED = 2000, 7, 20261117   # block bootstrap over issue dates


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
    # Continuous calendar. Short gaps (<= 3 days) are carried forward from the
    # last known value only, never interpolated from later days, so no level
    # ever contains future information. Longer gaps stay NaN and are skipped.
    df = df.asfreq("D").ffill(limit=3)
    return df, meta


def _norm_strait(s) -> str:
    return str(s or "").strip().lower().replace("-", "_").replace(" ", "_")


def load_incidents(ledger_path: Path, strait: str, end: pd.Timestamp) -> tuple[int | None, int]:
    """(independently reported incidents in the 7 days to `end`, claims-only count).
    None when no usable ledger exists. Example rows and rows that name no strait
    (or an unknown one) are counted for neither strait."""
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
        if _norm_strait(r.get("strait")) not in STRAIT_ALIASES[strait]:
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


class Forecaster:
    """A level function + empirical error intervals, evaluated without look-ahead."""

    def __init__(self, name: str, levels: pd.Series, y: pd.Series):
        self.name = name
        self.lev = levels.to_numpy(dtype=float)
        self.fut = future_matrix(y)
        self.err = self.fut - self.lev[:, None]
        self.err_week = self.fut.sum(axis=1) - HORIZON * self.lev  # NaN if any day missing

    def offsets(self, i: int):
        """10th/90th percentile of daily and weekly-sum errors from issue dates whose
        whole 7-day outcome was known at issue index i (j + 7 <= i)."""
        hi = i - HORIZON
        lo = max(0, hi - ERROR_WINDOW + 1)
        if hi < lo:
            return None
        d = self.err[lo: hi + 1].ravel()
        d = d[~np.isnan(d)]
        w = self.err_week[lo: hi + 1]
        w = w[~np.isnan(w)]
        if len(d) < 30 or len(w) < 10:
            return None
        return (float(np.quantile(d, Q_LO)), float(np.quantile(d, Q_HI)),
                float(np.quantile(w, Q_LO)), float(np.quantile(w, Q_HI)))

    def predict(self, i: int) -> dict | None:
        lv = self.lev[i]
        off = None if np.isnan(lv) else self.offsets(i)
        if off is None:
            return None
        dlo, dhi, wlo, whi = off
        return {"p50": max(lv, 0.0), "p10": max(lv + dlo, 0.0), "p90": max(lv + dhi, 0.0),
                "w50": max(HORIZON * lv, 0.0),
                "w10": max(HORIZON * lv + wlo, 0.0), "w90": max(HORIZON * lv + whi, 0.0)}

    def score_rows(self, idx) -> tuple[np.ndarray, np.ndarray]:
        """Per issue date: abs errors (len(idx) x 7) and 'inside p10-p90' flags.
        NaN where there is no prediction or no outcome."""
        ae = np.full((len(idx), HORIZON), np.nan)
        ins = np.full((len(idx), HORIZON), np.nan)
        for k, i in enumerate(idx):
            p = self.predict(i)
            if p is None:
                continue
            a = self.fut[i]
            ok = ~np.isnan(a)
            ae[k, ok] = np.abs(a[ok] - p["p50"])
            ins[k, ok] = ((a[ok] >= p["p10"]) & (a[ok] <= p["p90"])).astype(float)
        return ae, ins


def mae_cov(ae: np.ndarray, ins: np.ndarray) -> tuple[float | None, float | None, int]:
    n = int((~np.isnan(ae)).sum())
    if n == 0:
        return None, None, 0
    return float(np.nanmean(ae)), float(np.nanmean(ins)), n


def paired_gain_ci(ae_model: np.ndarray, ae_naive: np.ndarray) -> tuple[float | None, float | None, float | None]:
    """MAE gain of the model over naive (naive MAE - model MAE, transits/day) with a
    95% CI from a moving-block bootstrap over issue dates. Errors at the 7 horizons
    of one issue date, and of neighbouring issue dates, are correlated; resampling
    blocks of 7 consecutive issue dates keeps that correlation in the CI."""
    both = ~np.isnan(ae_model) & ~np.isnan(ae_naive)
    keep = both.any(axis=1)
    m = np.where(both, ae_model, 0.0)[keep]
    nv = np.where(both, ae_naive, 0.0)[keep]
    cnt = both[keep].sum(axis=1).astype(float)
    n = len(cnt)
    if n < 2 * BOOT_BLOCK:
        return None, None, None
    sm, sn = m.sum(axis=1), nv.sum(axis=1)
    point = float((sn.sum() - sm.sum()) / cnt.sum())
    rng = np.random.default_rng(BOOT_SEED)
    n_blocks = math.ceil(n / BOOT_BLOCK)
    starts = rng.integers(0, n - BOOT_BLOCK + 1, size=(BOOT_N, n_blocks))
    rows = (starts[:, :, None] + np.arange(BOOT_BLOCK)).reshape(BOOT_N, -1)[:, :n]
    gains = (sn[rows].sum(axis=1) - sm[rows].sum(axis=1)) / cnt[rows].sum(axis=1)
    lo, hi = np.quantile(gains, [0.025, 0.975])
    return point, float(lo), float(hi)


def choose_half_life(y: pd.Series, tune_idx) -> float:
    best, best_mae = HALF_LIVES[0], math.inf
    for hl in HALF_LIVES:
        mae, _, _ = mae_cov(*Forecaster("x", ewma_levels(y, hl), y).score_rows(tune_idx))
        if mae is not None and mae < best_mae:
            best, best_mae = hl, mae
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
                       + (f"; {claims} further claim(s) not independently confirmed and not counted."
                          if claims else "."))
        if incidents >= 1 and label == "open":
            label = "contested"
            reasons.append("Raised from open to contested because of recent independently reported incidents.")
    return label, reasons


# ------------------------------------------------------------------- forecast
def _rd(x, nd=3):
    return None if x is None or (isinstance(x, float) and math.isnan(x)) else round(float(x), nd)


def r1(x: float) -> float:
    return round(float(x), 1)


def windows(n: int) -> dict:
    """Index ranges of the three issue-date windows. A window's last issue date
    has its outcome 7 days later, so each next window starts HORIZON days on."""
    last_eval = n - 1 - HORIZON
    rep = range(max(0, last_eval - REPORT_DAYS + 1), last_eval + 1)
    sel_end = rep.start - HORIZON                      # outcomes end before REPORT starts
    sel = range(max(0, sel_end - SELECT_DAYS + 1), max(0, sel_end + 1))
    tune_end = sel.start - HORIZON
    tune = range(max(0, tune_end - TUNE_DAYS + 1), max(0, tune_end + 1))
    return {"tune": tune, "select": sel, "report": rep}


def choose(y: pd.Series, tune_idx, select_idx) -> dict:
    """The full procedure: tune the half-life on tune_idx, then pick model vs the
    better naive on select_idx with the paired bootstrap CI rule."""
    hl = choose_half_life(y, tune_idx)
    fcs = {"model": Forecaster(f"ewma_hl{hl:g}", ewma_levels(y, hl), y),
           "naive_ma7": Forecaster("naive_ma7", ma7_levels(y), y),
           "naive_last": Forecaster("naive_last", last_levels(y), y)}
    sc = {k: f.score_rows(select_idx) for k, f in fcs.items()}
    mae = {k: mae_cov(*v)[0] for k, v in sc.items()}
    nk = min(("naive_ma7", "naive_last"), key=lambda k: mae[k] if mae[k] is not None else math.inf)
    gain, lo, hi = paired_gain_ci(sc["model"][0], sc[nk][0])
    if lo is not None and lo > 0:
        outcome = "model_clearly_better"
    elif hi is not None and hi < 0:
        outcome = "naive_clearly_better"
    else:
        outcome = "no_meaningful_difference"
    used = "model" if outcome == "model_clearly_better" else nk
    return {"fcs": fcs, "half_life": hl, "naive_key": nk, "used": used, "outcome": outcome,
            "mae_model": mae["model"], "mae_naive": mae[nk], "gain": gain, "ci": (lo, hi)}


def forecast_series(y: pd.Series, w: dict, issue_i: int) -> dict:
    """live  = choose(tune=SELECT, select=REPORT): today's method, from the most recent data.
    check = choose(tune=TUNE, select=SELECT), then scored on REPORT, which it never saw:
            an honest out-of-sample test of the choose-then-forecast procedure."""
    live = choose(y, w["select"], w["report"])
    check = choose(y, w["tune"], w["select"])
    pred = live["fcs"][live["used"]].predict(issue_i)
    if pred is None:  # not enough history for intervals: flat naive point
        lv = float(ma7_levels(y).iloc[issue_i])
        pred = {"p50": lv, "p10": lv, "p90": lv, "w50": 7 * lv, "w10": 7 * lv, "w90": 7 * lv}
    rep_live = {k: mae_cov(*live["fcs"][k].score_rows(w["report"])) for k in ("model", live["naive_key"])}
    rep_used = mae_cov(*live["fcs"][live["used"]].score_rows(w["report"]))
    chk_choice = mae_cov(*check["fcs"][check["used"]].score_rows(w["report"]))
    chk_naive = mae_cov(*check["fcs"][check["naive_key"]].score_rows(w["report"]))
    return {"pred": pred, "live": live, "check": check, "rep_live": rep_live, "rep_used": rep_used,
            "chk_choice": chk_choice, "chk_naive": chk_naive}


def _win(y: pd.Series, r: range) -> dict:
    if len(r) == 0:
        return {"start": None, "end": None}
    return {"start": y.index[r.start].date().isoformat(), "end": y.index[r.stop - 1].date().isoformat()}


def _label(key: str) -> str:
    return "model" if key == "model" else "naive_baseline"


def series_backtest(y: pd.Series, w: dict, f: dict) -> dict:
    L, C = f["live"], f["check"]
    nk = L["naive_key"]
    lo, hi = L["ci"]
    clo, chi = C["ci"]
    return {
        # contract fields (data/README.md): most recent window, horizons 1-7
        "mae_model": _rd(f["rep_live"]["model"][0]),
        "mae_naive": _rd(f["rep_live"][nk][0]),
        "coverage_80": _rd(f["rep_used"][1]),          # of the method shown today
        "window": _win(y, w["report"]),
        # how today's method was chosen (on that same most recent window)
        "method_used": _label(L["used"]),
        "naive_kind": nk,
        "outcome": L["outcome"],
        "gain": _rd(L["gain"]), "gain_ci95": [_rd(lo), _rd(hi)],
        "rule": "model shown only if the whole 95% CI of (naive MAE - model MAE) is above 0",
        "half_life_days": L["half_life"],
        "forecast_points": f["rep_live"]["model"][2],
        # honest check: the same procedure run one window earlier, scored on data it never saw
        "out_of_sample_check": {
            "tuned_on": _win(y, w["tune"]), "chosen_on": _win(y, w["select"]), "scored_on": _win(y, w["report"]),
            "chose": _label(C["used"]), "outcome_then": C["outcome"],
            "gain_then": _rd(C["gain"]), "gain_ci95_then": [_rd(clo), _rd(chi)],
            "mae_of_choice": _rd(f["chk_choice"][0]), "mae_naive": _rd(f["chk_naive"][0]),
            "coverage_80_of_choice": _rd(f["chk_choice"][1]),
        },
    }


def verdict_text(f: dict, y: pd.Series, w: dict) -> str:
    L, C = f["live"], f["check"]
    lo, hi = L["ci"]
    nk = L["naive_key"].replace("naive_", "naive ")
    ci = f"gain {L['gain']:+.2f} transits/day, 95% CI {lo:+.2f} to {hi:+.2f}" if lo is not None else "CI unavailable"
    rw = _win(y, w["report"])
    if L["outcome"] == "model_clearly_better":
        head = f"Model is clearly better than the {nk} baseline over {rw['start']} to {rw['end']} ({ci}); the model is shown."
    elif L["outcome"] == "naive_clearly_better":
        head = f"The {nk} baseline is clearly better than the model over {rw['start']} to {rw['end']} ({ci}); the baseline is shown."
    else:
        head = (f"No meaningful difference between the model and the {nk} baseline over "
                f"{rw['start']} to {rw['end']} ({ci}); the simpler baseline is shown.")
    cm, cn = f["chk_choice"][0], f["chk_naive"][0]
    tail = ""
    if cm is not None and cn is not None:
        chose = _label(C["used"]).replace("_", " ")
        tail = (f" Out-of-sample check: the same rule applied one quarter earlier chose the {chose}, "
                f"which then had MAE {cm:.2f} vs {cn:.2f} for naive on the most recent window.")
    return head + tail


def run_strait(strait: str, data_dir: Path, now: dt.datetime) -> dict:
    df, meta = load_transits(data_dir / "transits" / f"{strait}.json")
    df = df.loc[: df["total"].last_valid_index()]
    y = df["total"]
    n = len(y)
    issue_i = n - 1
    last_date = y.index[issue_i]
    w = windows(n)

    total = forecast_series(y, w, issue_i)
    p = total["pred"]
    dates = [(last_date + pd.Timedelta(days=h)).date().isoformat() for h in range(1, HORIZON + 1)]

    by_type = {}
    for t in SHIP_TYPES:
        s = df[t]
        if s.notna().sum() < TUNE_DAYS + SELECT_DAYS + REPORT_DAYS:
            continue
        ft = forecast_series(s, w, issue_i)
        q = ft["pred"]
        by_type[t] = {
            # p10/p50/p90 are 7-day totals (data/README.md)
            "p10": r1(q["w10"]), "p50": r1(q["w50"]), "p90": r1(q["w90"]),
            "daily": [{"date": d, "p10": r1(q["p10"]), "p50": r1(q["p50"]), "p90": r1(q["p90"])} for d in dates],
            "method": _label(ft["live"]["used"]),
            "backtest": series_backtest(s, w, ft),
        }

    # Regime: today's label plus the last 30 days, each computed only from data to that day.
    baseline_cfg = BASELINES[strait]
    base_val = float(y[baseline_cfg["start"]: baseline_cfg["end"]].mean())
    ledger = data_dir / "ledger.json"
    history = []
    for i in range(max(0, n - 30), n):
        inc, cl = load_incidents(ledger, strait, y.index[i])
        lab, _ = regime_for(y, i, base_val, inc, cl, STRAIT_NAMES[strait], baseline_cfg["label"])
        history.append({"date": y.index[i].date().isoformat(), "regime": lab})
    inc, cl = load_incidents(ledger, strait, last_date)
    regime, reasons = regime_for(y, issue_i, base_val, inc, cl, STRAIT_NAMES[strait], baseline_cfg["label"])

    bt = series_backtest(y, w, total)
    bt["verdict"] = verdict_text(total, y, w)
    L = total["live"]
    rep_w = bt["window"]
    if L["used"] == "model":
        method_note = (f"Forecast = recent daily transits averaged with weights that halve every "
                       f"{L['half_life']:g} days. Shown because it beat a naive baseline clearly (95% confidence) "
                       f"over {rep_w['start']} to {rep_w['end']}. Range = 10th-90th percentile of its past errors.")
    else:
        what = "the last 7-day average" if L["naive_key"] == "naive_ma7" else "yesterday's count"
        how = ("did worse than it" if L["outcome"] == "naive_clearly_better"
               else "showed no clear gain over it")
        method_note = (f"Forecast = {what}, repeated for 7 days. A weighted-average model {how} "
                       f"from {rep_w['start']} to {rep_w['end']}, so the simpler method is shown. "
                       f"Range = 10th-90th percentile of its past errors.")

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
        "by_type_note": ("Each ship type is forecast separately, so the per-type p50s need not add up "
                         "to the total p50, and p10/p90 never add up (ranges do not sum)."),
        "method": _label(L["used"]),
        "backtest": bt,
        "method_note": method_note,
        "baseline": {"mean_per_day": r1(base_val), "period": f"{baseline_cfg['start']} to {baseline_cfg['end']}",
                     "label": baseline_cfg["label"]},
        "note": "Forecast dates start the day after the last settled PortWatch day. Not financial advice.",
    }


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
            words = len(res["method_note"].split())
            if words >= 60:
                raise ValueError(f"method_note too long ({words} words)")
        except Exception as e:  # keep the other strait; non-zero exit lets refresh.sh restore data/
            print(f"[forecast] {strait}: FAILED: {e}", file=sys.stderr)
            failed += 1
            continue
        tmp = out_dir / f".{strait}.json.tmp"
        tmp.write_text(json.dumps(res, indent=2), encoding="utf-8")
        tmp.replace(out_dir / f"{strait}.json")
        b = res["backtest"]
        print(f"[forecast] {strait}: regime={res['regime']} week p50={res['weekly_total']['p50']} "
              f"gain={b['gain']} CI={b['gain_ci95']} -> {res['method']} ({b['outcome']}); "
              f"MAE model={b['mae_model']} naive={b['mae_naive']} cov80={b['coverage_80']}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
