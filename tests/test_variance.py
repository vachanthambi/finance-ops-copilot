"""
Tests for the variance decomposition.

Two layers. Synthetic cases construct a scenario where exactly one driver moved
and assert the engine attributes it to that driver and nothing else — this is
what catches a subtly wrong mix formula. Then the real database is checked for
the controlling identity and the planted overrun.

Run:  pytest tests/test_variance.py -v
"""

import json
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from engine.variance import (add_fx_effect, assert_identity, decompose,
                             get_engine, run_variance)

ROOT = Path(__file__).resolve().parents[1]


def frame(rows):
    """Build a decomposition input frame from (product, aq, aamt, bq, bamt)."""
    return pd.DataFrame([{
        "cost_center_id": 1,
        "period_month": date(2024, 1, 1),
        "product_line": p,
        "actual_qty": aq,
        "actual_cc_amount": aamt,
        "actual_amount": aamt,          # no FX movement in synthetic cases
        "budget_qty": bq,
        "budget_amount": bamt,
    } for p, aq, aamt, bq, bamt in rows])


def run(rows):
    return assert_identity(add_fx_effect(decompose(frame(rows))))


# ------------------------------------------------------------------
# Synthetic: one driver at a time
# ------------------------------------------------------------------

class TestSyntheticDrivers:

    def test_on_plan_is_all_zeros(self):
        d = run([("A", 100, 1000.0, 100, 1000.0)])
        assert d["total_variance"].sum() == pytest.approx(0.0)
        for col in ["volume_variance", "mix_variance", "price_variance",
                    "fx_variance"]:
            assert d[col].sum() == pytest.approx(0.0, abs=1e-6)

    def test_pure_volume(self):
        """20% more units at the planned price is volume and nothing else."""
        d = run([("A", 120, 1200.0, 100, 1000.0)])
        assert d["volume_variance"].sum() == pytest.approx(200.0)
        assert d["mix_variance"].sum() == pytest.approx(0.0, abs=1e-6)
        assert d["price_variance"].sum() == pytest.approx(0.0, abs=1e-6)

    def test_pure_price(self):
        """Planned units at a higher realised price is price and nothing else."""
        d = run([("A", 100, 1200.0, 100, 1000.0)])
        assert d["price_variance"].sum() == pytest.approx(200.0)
        assert d["volume_variance"].sum() == pytest.approx(0.0, abs=1e-6)
        assert d["mix_variance"].sum() == pytest.approx(0.0, abs=1e-6)

    def test_pure_mix(self):
        """
        Same total units, shifted toward the more expensive line.

        Plan: 100 of A at 10, 100 of B at 20  -> 3,000 on 200 units
        Actual: 50 of A, 150 of B at plan prices -> 3,500 on 200 units
        The entire 500 gap is composition.
        """
        d = run([
            ("A", 50, 500.0, 100, 1000.0),
            ("B", 150, 3000.0, 100, 2000.0),
        ])
        assert d["total_variance"].sum() == pytest.approx(500.0)
        assert d["mix_variance"].sum() == pytest.approx(500.0)
        assert d["volume_variance"].sum() == pytest.approx(0.0, abs=1e-6)
        assert d["price_variance"].sum() == pytest.approx(0.0, abs=1e-6)

    def test_volume_and_price_together(self):
        d = run([("A", 120, 1440.0, 100, 1000.0)])
        assert d["volume_variance"].sum() == pytest.approx(200.0)
        assert d["price_variance"].sum() == pytest.approx(240.0)
        assert d["total_variance"].sum() == pytest.approx(440.0)

    def test_fx_isolated(self):
        """Constant-currency flat, reported up: the whole gap is currency."""
        df = frame([("A", 100, 1000.0, 100, 1000.0)])
        df["actual_amount"] = 1100.0        # reported USD higher than CC
        d = assert_identity(add_fx_effect(decompose(df)))
        assert d["fx_variance"].sum() == pytest.approx(100.0)
        assert d["total_variance"].sum() == pytest.approx(100.0)
        assert d["volume_variance"].sum() == pytest.approx(0.0, abs=1e-6)

    def test_unbudgeted_product_falls_to_volume(self):
        d = run([("A", 100, 1000.0, 100, 1000.0),
                 ("B", 40, 800.0, 0, 0.0)])
        assert d.loc[d["product_line"] == "B", "volume_variance"].iloc[0] \
            == pytest.approx(800.0)
        assert d.loc[d["product_line"] == "B", "price_variance"].iloc[0] \
            == pytest.approx(0.0)

    def test_identity_holds_on_random_data(self):
        import random
        random.seed(7)
        rows = []
        for i in range(30):
            bq = random.randint(10, 500)
            bp = random.uniform(20, 300)
            aq = int(bq * random.uniform(0.5, 1.6))
            ap = bp * random.uniform(0.7, 1.4)
            rows.append((f"P{i % 5}", aq, aq * ap, bq, bq * bp))
        d = run(rows)          # assert_identity raises if the split is wrong
        assert len(d) == 30


# ------------------------------------------------------------------
# Against the real database
# ------------------------------------------------------------------

@pytest.fixture(scope="session")
def manifest():
    return json.loads((ROOT / "data" / "defects_manifest.json")
                      .read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def result():
    return run_variance(get_engine())


class TestAgainstDatabase:

    def test_identity_holds_everywhere(self, result):
        gap = result["detail"]["identity_gap"].abs().max()
        assert gap < 0.01, f"largest identity gap {gap:,.4f} USD"

    def test_bridge_reconciles(self, result):
        b = result["bridge"]
        walked = b["budget"] + b["volume"] + b["mix"] + b["price"] + b["fx"]
        assert walked == pytest.approx(b["actual"], abs=1.0)

    def test_planted_overrun_detected(self, result, manifest):
        """The seeded 40% overrun must show up as a large positive variance."""
        ov = manifest["defects"]["budget_overrun"]
        target_month = pd.to_datetime(ov["period_month"]).date()

        res = run_variance(get_engine(),
                           period_month=target_month,
                           cost_center_id=ov["cost_center_id"])
        pct = res["bridge"]["variance_pct"]
        assert 35 <= pct <= 45, f"overrun measured at {pct:+.1f}%, expected ~+40%"

    def test_overrun_is_operational_not_fx(self, result, manifest):
        """
        The overrun was seeded as real trading, so volume should dominate.
        If FX carried it, the constant-currency restatement is wrong.
        """
        ov = manifest["defects"]["budget_overrun"]
        target_month = pd.to_datetime(ov["period_month"]).date()
        res = run_variance(get_engine(),
                           period_month=target_month,
                           cost_center_id=ov["cost_center_id"])
        b = res["bridge"]
        operating = abs(b["volume"]) + abs(b["mix"]) + abs(b["price"])
        assert operating > abs(b["fx"]), \
            "currency effect outweighs operating drivers on a trading overrun"

    def test_every_row_has_a_region(self, result):
        assert result["detail"]["region"].isna().sum() == 0
