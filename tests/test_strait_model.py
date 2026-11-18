"""Unit tests for the strait-level count model (ticket #27).

    py -m unittest discover -s tests -v

Synthetic data only; no network.
"""
import json
import math
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "scripts"))
import strait_model as SM  # noqa: E402
import forecast as F  # noqa: E402

ALIASES = {"hormuz", "strait_of_hormuz"}


def synth(n=500, seed=1, mean=20.0, alpha=0.0):
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2025-01-01", periods=n, freq="D")
    lam = mean * np.ones(n)
    lam[n // 2:] *= 0.5                                   # a regime drop half-way
    if alpha > 0:
        lam = rng.gamma(1 / alpha, lam * alpha)
    return pd.Series(rng.poisson(lam).astype(float), index=idx)


def ctx_for(y, inc=None):
    inc = pd.Series(0.0, index=y.index) if inc is None else inc
    return SM.Context(y, baseline=float(y.iloc[:100].mean()), incidents=inc, open_ratio=0.7, closed_ratio=0.1)


class TestDistributions(unittest.TestCase):
    def test_pmf_moments(self):
        for mu, a in ((3.7, 0.0), (27.0, 0.0), (85.0, 0.05), (2.0, 0.5)):
            p = SM.pmf(mu, a, SM._kmax(mu, a))
            k = np.arange(len(p))
            self.assertAlmostEqual(p.sum(), 1.0, places=6)
            m = float((k * p).sum())
            v = float(((k - m) ** 2 * p).sum())
            self.assertAlmostEqual(m, mu, places=3)
            self.assertAlmostEqual(v, mu + a * mu * mu, delta=1e-2 * (mu + a * mu * mu))

    def test_crps_discrete_matches_sample(self):
        rng = np.random.default_rng(0)
        for mu, a, y in ((4.0, 0.0, 2), (25.0, 0.1, 31)):
            x = rng.poisson(rng.gamma(1 / a, mu * a, 200000)) if a else rng.poisson(mu, 200000)
            self.assertAlmostEqual(SM.crps_count(mu, a, y), SM.crps_sample(x, y), delta=0.03)

    def test_quantiles_and_weekly(self):
        lo, med, hi = SM.quantiles(3.74, 0.0)
        self.assertEqual(med, 4.0)                        # Poisson(3.74) median
        self.assertTrue(lo <= med <= hi)
        m, a = SM.weekly_params(np.full(7, 3.0), 0.0)
        self.assertEqual((m, a), (21.0, 0.0))
        m, a = SM.weekly_params(np.full(7, 3.0), 0.2)
        self.assertAlmostEqual(m + a * m * m, 21 + 0.2 * 63, places=6)


class TestFitting(unittest.TestCase):
    def test_irls_recovers_poisson_coefficients(self):
        rng = np.random.default_rng(3)
        x = rng.normal(size=4000)
        X = np.column_stack([np.ones_like(x), x])
        y = rng.poisson(np.exp(1.5 + 0.4 * x)).astype(float)
        b = SM.irls(X, y, np.ones_like(y), ridge=0.0)
        self.assertAlmostEqual(b[0], 1.5, delta=0.03)
        self.assertAlmostEqual(b[1], 0.4, delta=0.03)

    def test_dispersion_test(self):
        rng = np.random.default_rng(5)
        mu = np.full(5000, 20.0)
        w = np.ones_like(mu)
        a0, z0 = SM.dispersion(rng.poisson(mu).astype(float), mu, w)
        self.assertLess(z0, SM.DISPERSION_Z)
        y = rng.poisson(rng.gamma(1 / 0.3, mu * 0.3)).astype(float)
        a1, z1 = SM.dispersion(y, mu, w)
        self.assertGreater(z1, SM.DISPERSION_Z)
        self.assertAlmostEqual(a1, 0.3, delta=0.05)

    def test_family_choice(self):
        self.assertEqual(SM.CountModel(ctx_for(synth(alpha=0.0)), 56).fit(450)["family"], "poisson")
        self.assertEqual(SM.CountModel(ctx_for(synth(alpha=0.3)), 56).fit(450)["family"], "negative_binomial")


class TestNoLookAhead(unittest.TestCase):
    def test_future_values_do_not_move_the_forecast(self):
        y = synth(seed=7)
        i = 420
        before = SM.CountModel(ctx_for(y), 28).predict(i)
        y2 = y.copy()
        y2.iloc[i + 1:] = y2.iloc[i + 1:] * 5 + 100       # wreck the future
        after = SM.CountModel(ctx_for(y2), 28).predict(i)
        self.assertEqual(before, after)

    def test_incidents_count_from_the_day_after(self):
        idx = pd.date_range("2026-11-01", periods=10, freq="D")
        rows = [{"strait": "hormuz", "date": "2026-11-03", "status": "confirmed", "independent_source": "UKMTO"},
                {"strait": "hormuz", "date": "2026-11-03", "status": "claimed_only", "independent_source": None},
                {"strait": "bab_el_mandeb", "date": "2026-11-03", "status": "confirmed"},
                {"strait": "hormuz", "date": "2026-11-03", "status": "confirmed", "example": True},
                {"strait": None, "date": "2026-11-03", "status": "confirmed"}]
        s = SM.incident_series(rows, ALIASES, idx)
        self.assertEqual(s["2026-11-03"], 0)             # not known on the event day itself
        self.assertEqual(s["2026-11-04"], 1)
        self.assertEqual(s["2026-11-10"], 1)             # 7-day window 4..10 Nov
        self.assertEqual(s.sum(), 7)

    def test_windows_are_ordered_and_separated(self):
        w = F.windows(1000)
        self.assertGreaterEqual(w["select"].start - (w["tune"].stop - 1), F.HORIZON)
        self.assertGreaterEqual(w["report"].start - (w["select"].stop - 1), F.HORIZON)


class TestShares(unittest.TestCase):
    def test_type_means_add_up_to_total(self):
        rng = np.random.default_rng(9)
        n = 400
        idx = pd.date_range("2025-01-01", periods=n, freq="D")
        a = pd.Series(rng.poisson(6, n).astype(float), index=idx)
        b = pd.Series(rng.poisson(14, n).astype(float), index=idx)
        tot = a + b
        m = SM.CountModel(ctx_for(tot), 28)
        sa, sb = SM.ShareModel(m, a), SM.ShareModel(m, b)
        i = 350
        mu_t = m.dist(i)[0]
        self.assertTrue(np.allclose(sa.dist(i)[0] + sb.dist(i)[0], mu_t))


class TestForecastOutput(unittest.TestCase):
    def test_run_strait_contract(self):
        y = synth(n=700, seed=11, mean=30.0)
        rows = [{"date": d.date().isoformat(), "total": float(v),
                 "by_type": {"tanker": float(v // 2), "container": None, "dry_bulk": None,
                             "general_cargo": None, "roro": None}} for d, v in y.items()]
        with tempfile.TemporaryDirectory() as td:
            dd = Path(td)
            (dd / "transits").mkdir()
            (dd / "transits" / "hormuz.json").write_text(json.dumps({"daily": rows}), encoding="utf-8")
            saved = F.BASELINES["hormuz"]
            F.BASELINES["hormuz"] = {"start": "2025-01-01", "end": "2025-06-30", "label": "test baseline"}
            try:
                res = F.run_strait("hormuz", dd, pd.Timestamp("2026-12-01", tz="UTC").to_pydatetime())
            finally:
                F.BASELINES["hormuz"] = saved
        for k in ("issued_at", "regime", "regime_reasons", "horizon_days", "daily", "weekly_total",
                  "by_type", "backtest", "method_note", "method", "target_note"):
            self.assertIn(k, res)
        self.assertEqual(len(res["daily"]), 7)
        self.assertIn("PortWatch", res["method_note"])
        self.assertLess(len(res["method_note"].split()), 60)
        b = res["backtest"]
        for k in ("mae_model", "mae_naive", "coverage_80", "gain_ci95", "crps_model", "crps_naive",
                  "crps_gain_ci95", "log_score_model", "models_compared", "count_model", "outcome"):
            self.assertIn(k, b)
        self.assertEqual(b["method_used"] == "model", b["outcome"] == "model_clearly_better")
        self.assertIn(b["count_model"]["family"], ("poisson", "negative_binomial"))
        self.assertNotIn("CRPS", b["rule"])                       # total: MAE decides
        tb = res["by_type"]["tanker"]["backtest"]
        self.assertIn("CRPS", tb["rule"])                         # per type: MAE and CRPS both
        if tb["outcome"] == "model_clearly_better":
            self.assertGreater(tb["gain_ci95"][0], 0)
        for d in res["daily"]:
            self.assertGreaterEqual(d["p10"], 0)
            self.assertLessEqual(d["p10"], d["p50"])
            self.assertLessEqual(d["p50"], d["p90"])


if __name__ == "__main__":
    unittest.main()
