#!/usr/bin/env python3
"""ML Phase 1: strait-level count model for daily transits (ticket #27).

A small, explainable generalised linear model for the PortWatch daily count,
used by scripts/forecast.py as the "model" that must clearly beat the naive
baseline before the page shows it (user rule, 17 Nov 2026).

Target   PortWatch's daily transit count (what the markets settle on). It is
         AIS-based and undercounts real traffic: for 5-11 Nov 2026 UKMTO reported
         308 Hormuz transits (82 AIS-observed) against PortWatch's 27.
Family   negative binomial, or Poisson when the Cameron-Trivedi dispersion test
         finds no overdispersion (refit at every issue date; the test uses one
         row per target day, the horizon-1 rows, because the 7 horizons overlap).
Mean     log mu[i, h] = b0 + b1 * log(7-day mean at i)
                       + b2 * (log 28-day mean - log 7-day mean)      (trend)
                       + weekday of the target day (6 dummies)
                       + regime at i (contested, closed dummies)       (lagged label)
                       + log(1 + independently reported incidents in the 7 days to i)
         Fitted by penalised IRLS (ridge on everything except intercept and
         level) on the last 365 issue dates whose outcome was already known at i,
         weighted by 0.5 ** (age / half-life); the half-life is tuned on the
         tuning window exactly as #3 tunes its smoother.
Types    total forecast x smoothed type share (last 28 days, shrunk towards the
         last 91 days), so the type means add up to the total mean.
Excluded ACLED (EULA bars ML use) and Global Fishing Watch (non-commercial).
         The UKMTO weekly dark-share figure is not an input: there is no
         machine-readable history (PDF reports, ~15 so far) and it describes true
         traffic, not the PortWatch series being forecast.

No look-ahead: every feature at issue date i uses data up to and including day
i; incidents count only from the day after their event date; training rows
(j, h) need j + h <= i. tests/test_strait_model.py checks this by changing
future values and asserting the forecast at i does not move.
"""
from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pandas as pd

HORIZON = 7
TRAIN_MAX_DAYS = 365
WEIGHT_HALF_LIVES = (14, 28, 56, 112)        # days, tuned like #3's smoother
RIDGE = 1.0                                   # penalty on non-level coefficients
INCIDENT_REPORT_LAG = 1                       # an incident counts from the day after it
SHARE_DAYS, SHARE_PRIOR_DAYS, SHARE_PRIOR_SHIPS = 28, 91, 20.0
Q_LO, Q_HI = 0.10, 0.90
DISPERSION_Z = 1.645                          # one-sided 5% test for overdispersion
MIN_TRAIN_ROWS = 60

FEATURES = ("intercept", "level", "trend", "dow_mon", "dow_tue", "dow_wed", "dow_thu",
            "dow_fri", "dow_sat", "regime_contested", "regime_closed", "incidents_7d")
_PENALISED = np.array([0, 0] + [1] * (len(FEATURES) - 2), dtype=float)


# ------------------------------------------------------------------ ledger
def classify_ledger_row(r: dict, aliases: set[str]) -> str | None:
    """'independent' | 'claim' | None (not for this strait, example, or no date).
    Same rule as #3: claimed_only, or an empty independent_source, is a claim."""
    if not isinstance(r, dict) or r.get("example") or r.get("is_example"):
        return None
    if str(r.get("id", "")).lower().startswith("example"):
        return None
    if str(r.get("strait") or "").strip().lower().replace("-", "_").replace(" ", "_") not in aliases:
        return None
    status = str(r.get("status", "")).lower()
    if ("claim" in status and "confirm" not in status) or (
            "independent_source" in r and not r.get("independent_source")):
        return "claim"
    return "independent"


def ledger_rows(ledger_path: Path) -> list[dict] | None:
    import json
    if not ledger_path.exists():
        return None
    try:
        raw = json.loads(ledger_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return raw if isinstance(raw, list) else next(
        (raw[k] for k in ("rows", "incidents", "entries", "data") if isinstance(raw.get(k), list)), [])


def incident_series(rows: list[dict] | None, aliases: set[str], index: pd.DatetimeIndex) -> pd.Series:
    """Independently reported incidents in the 7 days to each day, counted from the
    day after the event date (no same-day look-ahead). Zero where the ledger has no rows."""
    daily = pd.Series(0.0, index=index)
    for r in rows or []:
        if classify_ledger_row(r, aliases) != "independent":
            continue
        try:
            d = pd.Timestamp(r.get("date")).tz_localize(None).normalize()
        except (ValueError, TypeError):
            continue
        d = d + pd.Timedelta(days=INCIDENT_REPORT_LAG)
        if d in daily.index:
            daily[d] += 1
    return daily.rolling(7, min_periods=1).sum()


# ----------------------------------------------------------------- context
class Context:
    """Per-day features known at the end of each day."""

    def __init__(self, y: pd.Series, baseline: float, incidents: pd.Series,
                 open_ratio: float, closed_ratio: float):
        self.index = y.index
        self.y = y.to_numpy(dtype=float)
        ma7 = y.rolling(7, min_periods=4).mean()
        ma28 = y.rolling(28, min_periods=14).mean()
        self.level = np.log(ma7.to_numpy(dtype=float) + 0.5)
        tr = np.log(ma28.to_numpy(dtype=float) + 0.5) - self.level
        self.trend = np.where(np.isnan(tr), 0.0, tr)
        self.inc = incidents.reindex(y.index).fillna(0.0).to_numpy(dtype=float)
        ratio = ma7.to_numpy(dtype=float) / baseline if baseline > 0 else np.full(len(y), np.nan)
        reg = np.where(ratio >= open_ratio, 0, np.where(ratio >= closed_ratio, 1, 2))
        reg = np.where((reg == 0) & (self.inc >= 1), 1, reg)          # incidents: open -> contested
        self.regime = np.where(np.isnan(ratio), -1, reg)
        self.dow = y.index.dayofweek.to_numpy()                       # 0 = Monday

    def design(self, j: np.ndarray, h: np.ndarray) -> np.ndarray:
        X = np.zeros((len(j), len(FEATURES)))
        X[:, 0] = 1.0
        X[:, 1] = self.level[j]
        X[:, 2] = self.trend[j]
        tdow = (self.dow[j] + h) % 7
        for k in range(6):                                            # Mon..Sat, Sunday = reference
            X[:, 3 + k] = (tdow == k)
        X[:, 9] = self.regime[j] == 1
        X[:, 10] = self.regime[j] == 2
        X[:, 11] = np.log1p(self.inc[j])
        return X


# ------------------------------------------------------------- fitting
def irls(X: np.ndarray, y: np.ndarray, w: np.ndarray, alpha: float = 0.0,
         ridge: float = RIDGE, beta0: np.ndarray | None = None) -> np.ndarray:
    """Penalised IRLS for a log-link Poisson (alpha = 0) or NB2 mean model."""
    beta = np.zeros(X.shape[1]) if beta0 is None else beta0.copy()
    if beta0 is None:
        beta[0] = math.log(max(np.average(y, weights=w), 0.1))
        if X.shape[1] > 1:
            beta[0] -= np.average(X[:, 1], weights=w)
            beta[1] = 1.0
    pen = np.diag(ridge * _PENALISED[: X.shape[1]])
    for _ in range(50):
        eta = np.clip(X @ beta, -20, 20)
        mu = np.exp(eta)
        z = eta + (y - mu) / mu
        W = w * mu / (1.0 + alpha * mu)
        A = X.T @ (X * W[:, None]) + pen
        new = np.linalg.solve(A, X.T @ (W * z))
        if np.max(np.abs(new - beta)) < 1e-7:
            return new
        beta = new
    return beta


def dispersion(y: np.ndarray, mu: np.ndarray, w: np.ndarray) -> tuple[float, float]:
    """(alpha, z): weighted moment estimate of NB2 alpha and the Cameron-Trivedi
    statistic for H0: alpha = 0 (regress ((y-mu)^2 - y)/mu on mu, no intercept)."""
    num = float(np.sum(w * ((y - mu) ** 2 - mu)))
    den = float(np.sum(w * mu ** 2))
    alpha = max(num / den, 0.0) if den > 0 else 0.0
    t = ((y - mu) ** 2 - y) / mu
    sw = float(np.sum(w * mu ** 2))
    if sw <= 0:
        return alpha, 0.0
    a_hat = float(np.sum(w * mu * t) / sw)
    resid = t - a_hat * mu
    s2 = float(np.sum(w * resid ** 2) / np.sum(w))           # residual variance
    var = s2 * float(np.sum(w ** 2 * mu ** 2)) / sw ** 2      # var of the weighted LS slope
    se = math.sqrt(var) if var > 0 else 0.0
    return alpha, (a_hat / se if se > 0 else 0.0)


# ------------------------------------------------------- distributions
def pmf(mu: float, alpha: float, kmax: int) -> np.ndarray:
    """P(Y = 0..kmax) for Poisson (alpha = 0) or NB2(mean mu, var mu + alpha mu^2)."""
    k = np.arange(kmax + 1, dtype=float)
    mu = max(float(mu), 1e-9)
    lg = np.vectorize(math.lgamma)
    if alpha <= 1e-9:
        logp = k * math.log(mu) - mu - lg(k + 1)
    else:
        r = 1.0 / alpha
        logp = (lg(k + r) - lg(r) - lg(k + 1) + r * math.log(r / (r + mu)) + k * math.log(mu / (r + mu)))
    return np.exp(logp)


def _kmax(mu: float, alpha: float) -> int:
    sd = math.sqrt(mu + alpha * mu * mu)
    return int(mu + 12 * sd + 20)


def quantiles(mu: float, alpha: float, qs=(Q_LO, 0.5, Q_HI)) -> list[float]:
    cdf = np.cumsum(pmf(mu, alpha, _kmax(mu, alpha)))
    return [float(np.searchsorted(cdf, q - 1e-12)) for q in qs]


def crps_count(mu: float, alpha: float, y: float) -> float:
    """CRPS of a discrete count forecast: sum_k (F(k) - 1{y <= k})^2."""
    km = max(_kmax(mu, alpha), int(y) + 1)
    cdf = np.cumsum(pmf(mu, alpha, km))
    step = (np.arange(km + 1) >= y).astype(float)
    return float(np.sum((cdf - step) ** 2))


def log_score(mu: float, alpha: float, y: float) -> float:
    """Negative log predictive probability of the observed count (lower is better)."""
    k = int(round(y))
    p = pmf(mu, alpha, max(k, 1))[k]
    return float(-math.log(max(p, 1e-300)))


def crps_sample(x: np.ndarray, y: float) -> float:
    """CRPS of an empirical forecast distribution: E|X - y| - 0.5 E|X - X'|."""
    x = np.sort(np.asarray(x, dtype=float))
    n = len(x)
    if n == 0:
        return float("nan")
    term1 = float(np.mean(np.abs(x - y)))
    i = np.arange(1, n + 1)
    term2 = float(np.sum((2 * i - n - 1) * x)) / (n * n)
    return term1 - term2


def weekly_params(mus: np.ndarray, alpha: float) -> tuple[float, float]:
    """Moment-matched (mean, alpha) for the sum of 7 independent daily counts."""
    m = float(np.sum(mus))
    v = m + alpha * float(np.sum(mus ** 2))
    if v <= m + 1e-9 or m <= 0:
        return m, 0.0
    return m, (v - m) / (m * m)


# --------------------------------------------------------- forecasters
class CountModel:
    """Same interface as forecast.Forecaster (predict / score_rows) plus CRPS and
    log score. Refit at every issue date on data whose outcome was known then."""

    kind = "count_model"

    def __init__(self, ctx: Context, half_life: float):
        self.ctx = ctx
        self.hl = half_life
        self.name = f"count_hl{half_life:g}"
        self._fits: dict[int, dict | None] = {}
        n = len(ctx.y)
        self.fut = np.full((n, HORIZON), np.nan)
        for h in range(1, HORIZON + 1):
            self.fut[: n - h, h - 1] = ctx.y[h:]

    def fit(self, i: int) -> dict | None:
        if i in self._fits:
            return self._fits[i]
        c = self.ctx
        res = None
        if not np.isnan(c.level[i]):
            j = np.repeat(np.arange(max(0, i - TRAIN_MAX_DAYS), i), HORIZON)
            h = np.tile(np.arange(1, HORIZON + 1), len(j) // HORIZON)
            ok = (j + h <= i)
            j, h = j[ok], h[ok]
            yy = c.y[j + h]
            ok = ~np.isnan(yy) & ~np.isnan(c.level[j]) & (c.regime[j] >= 0)
            j, h, yy = j[ok], h[ok], yy[ok]
            if len(j) >= MIN_TRAIN_ROWS:
                X = c.design(j, h)
                w = 0.5 ** ((i - j) / self.hl)
                b = irls(X, yy, w)
                mu = np.exp(np.clip(X @ b, -20, 20))
                one = h == 1        # one row per target day: the 7 horizons overlap
                alpha, z = dispersion(yy[one], mu[one], w[one])
                family = "negative_binomial" if (z > DISPERSION_Z and alpha > 1e-6) else "poisson"
                if family == "negative_binomial":
                    b = irls(X, yy, w, alpha=alpha, beta0=b)
                    mu = np.exp(np.clip(X @ b, -20, 20))
                    alpha, _ = dispersion(yy[one], mu[one], w[one])
                else:
                    alpha = 0.0
                Xi = c.design(np.full(HORIZON, i), np.arange(1, HORIZON + 1))
                mus = np.exp(np.clip(Xi @ b, -20, 20))
                res = {"beta": b, "alpha": alpha, "z": z, "family": family, "mu": mus, "rows": int(len(j))}
        self._fits[i] = res
        return res

    def dist(self, i: int) -> tuple[np.ndarray, float] | None:
        f = self.fit(i)
        return None if f is None else (f["mu"], f["alpha"])

    def predict(self, i: int) -> dict | None:
        d = self.dist(i)
        if d is None:
            return None
        mus, a = d
        days = [quantiles(m, a) for m in mus]
        wm, wa = weekly_params(mus, a)
        w10, w50, w90 = quantiles(wm, wa)
        return {"p10": days[0][0], "p50": days[0][1], "p90": days[0][2],
                "days": [{"p10": q[0], "p50": q[1], "p90": q[2], "mean": float(m)} for q, m in zip(days, mus)],
                "w10": w10, "w50": w50, "w90": w90, "w_mean": wm}

    def _rows(self, idx, fn):
        out = np.full((len(idx), HORIZON), np.nan)
        for k, i in enumerate(idx):
            d = self.dist(i)
            if d is None:
                continue
            mus, a = d
            for hh in range(HORIZON):
                yv = self.fut[i, hh]
                if not np.isnan(yv):
                    out[k, hh] = fn(mus[hh], a, yv)
        return out

    def score_rows(self, idx):
        ae = np.full((len(idx), HORIZON), np.nan)
        ins = np.full((len(idx), HORIZON), np.nan)
        for k, i in enumerate(idx):
            p = self.predict(i)
            if p is None:
                continue
            for hh in range(HORIZON):
                yv = self.fut[i, hh]
                if np.isnan(yv):
                    continue
                q = p["days"][hh]
                ae[k, hh] = abs(yv - q["p50"])
                ins[k, hh] = float(q["p10"] <= yv <= q["p90"])
        return ae, ins

    def crps_rows(self, idx):
        return self._rows(idx, crps_count)

    def log_rows(self, idx):
        return self._rows(idx, log_score)

    def info(self, i: int) -> dict:
        f = self.fit(i) or {}
        return {"family": f.get("family"), "alpha": f.get("alpha"), "dispersion_z": f.get("z"),
                "train_rows": f.get("rows"), "train_weight_half_life_days": self.hl,
                "coefficients": ({k: round(float(v), 4) for k, v in zip(FEATURES, f["beta"])}
                                 if f.get("beta") is not None else None)}


class ShareModel(CountModel):
    """Per-type forecast = total count model x smoothed type share at issue date i."""

    kind = "count_model_share"

    def __init__(self, total: CountModel, share_y: pd.Series):
        self.total = total
        self.hl = total.hl
        self.name = f"share_of_{total.name}"
        self._fits = {}
        tv = total.ctx.y
        sv = share_y.to_numpy(dtype=float)
        n = len(sv)
        self.fut = np.full((n, HORIZON), np.nan)
        for h in range(1, HORIZON + 1):
            self.fut[: n - h, h - 1] = sv[h:]
        both = ~np.isnan(tv) & ~np.isnan(sv)
        ct = np.cumsum(np.where(both, sv, 0.0))
        cT = np.cumsum(np.where(both, tv, 0.0))

        def window_sum(c, i, d):
            return c[i] - (c[i - d] if i - d >= 0 else 0.0)
        self._share = np.full(n, np.nan)
        for i in range(n):
            T28, t28 = window_sum(cT, i, SHARE_DAYS), window_sum(ct, i, SHARE_DAYS)
            T91, t91 = window_sum(cT, i, SHARE_PRIOR_DAYS), window_sum(ct, i, SHARE_PRIOR_DAYS)
            prior = t91 / T91 if T91 > 0 else np.nan
            if np.isnan(prior):
                continue
            self._share[i] = (t28 + SHARE_PRIOR_SHIPS * prior) / (T28 + SHARE_PRIOR_SHIPS)

    def dist(self, i: int):
        d = self.total.dist(i)
        if d is None or np.isnan(self._share[i]):
            return None
        mus, a = d
        return mus * self._share[i], a

    def info(self, i: int) -> dict:
        out = self.total.info(i)
        out["share"] = None if np.isnan(self._share[i]) else round(float(self._share[i]), 4)
        return out


def tune(ctx: Context, tune_idx, score_fn) -> CountModel:
    """Pick the training-weight half-life with the lowest MAE on tune_idx."""
    best, best_mae = None, math.inf
    for hl in WEIGHT_HALF_LIVES:
        m = CountModel(ctx, hl)
        mae = score_fn(m, tune_idx)
        if mae is not None and mae < best_mae:
            best, best_mae = m, mae
    return best if best is not None else CountModel(ctx, WEIGHT_HALF_LIVES[1])
