"""
Deterministic budget-to-actual variance decomposition.

Splits the gap between plan and actual into four drivers a finance leader can
act on:

    volume  sold more or fewer units than planned
    mix     sold a different blend of products than planned
    price   realised a different price per unit than planned
    fx      currency moved between the plan rate and the period rate

The controlling identity, asserted in code rather than assumed:

    total variance = fx + volume + mix + price

FX is handled by constant-currency restatement. Actuals are converted back to
local currency at the period rate, then forward at the plan rate. The residual
is the currency effect; whatever is left is genuine operating performance.

No LLM in this module. Same database, same numbers, every time.
"""

import os
from datetime import date

import numpy as np
import pandas as pd
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

from engine.entity_resolution import build_crosswalk

# The rate the plan was set at. Everything is restated against this month.
PLAN_RATE_MONTH = date(2023, 1, 1)

# Rounding tolerance for the reconciliation identity, in USD.
IDENTITY_TOLERANCE = 0.01

GROUP_COLS = ["cost_center_id", "period_month"]


# ------------------------------------------------------------------
# Loading
# ------------------------------------------------------------------

def get_engine():
    load_dotenv()
    url = (
        f"postgresql+psycopg2://{os.getenv('DB_USER')}:{os.getenv('DB_PASSWORD')}"
        f"@{os.getenv('DB_HOST')}:{os.getenv('DB_PORT')}/{os.getenv('DB_NAME')}"
    )
    return create_engine(url)


def load_currency_map(engine) -> dict:
    """
    customer_id -> currency_code.

    The ledger is USD-only, so the trading currency has to come from CRM via
    the resolved identity map.
    """
    crm_accounts = pd.read_sql(text("SELECT * FROM crm.accounts"), engine)
    erp_customers = pd.read_sql(text("SELECT * FROM erp.customers"), engine)
    invoices = pd.read_sql(
        text("SELECT invoice_id, customer_email, customer_name_raw "
             "FROM billing.invoices"), engine)

    xwalk = build_crosswalk(crm_accounts, erp_customers, invoices)
    mapped = xwalk.crm_to_erp.merge(
        crm_accounts[["account_id", "currency_code"]], on="account_id")
    mapped = mapped[mapped["customer_id"].notna()]
    mapped = mapped.drop_duplicates("customer_id")
    return dict(zip(mapped["customer_id"].astype(int), mapped["currency_code"]))


def load_actuals(engine, currency_map: dict) -> pd.DataFrame:
    """
    GL revenue restated at constant currency.

    Conversion happens per entry, before aggregation, because customers inside
    one cost centre can trade in different currencies.
    """
    gl = pd.read_sql(text("""
        SELECT customer_id, cost_center_id, period_month, product_line,
               quantity, amount_usd
        FROM erp.gl_entries
    """), engine)
    fx = pd.read_sql(text("SELECT * FROM billing.fx_rates"), engine)

    gl["currency_code"] = gl["customer_id"].map(currency_map)

    period_rate = fx.rename(columns={"rate_month": "period_month",
                                     "rate_to_usd": "period_rate"})
    plan_rate = (fx[fx["rate_month"] == PLAN_RATE_MONTH]
                 [["currency_code", "rate_to_usd"]]
                 .rename(columns={"rate_to_usd": "plan_rate"}))

    gl = gl.merge(period_rate, on=["period_month", "currency_code"], how="left")
    gl = gl.merge(plan_rate, on="currency_code", how="left")

    # constant currency = local amount valued at the plan rate
    gl["amount_cc_usd"] = (gl["amount_usd"].astype(float)
                           * gl["plan_rate"].astype(float)
                           / gl["period_rate"].astype(float))

    return (gl.groupby(["cost_center_id", "product_line", "period_month"],
                       as_index=False)
              .agg(actual_qty=("quantity", "sum"),
                   actual_amount=("amount_usd", "sum"),
                   actual_cc_amount=("amount_cc_usd", "sum")))


def load_budget(engine) -> pd.DataFrame:
    return pd.read_sql(text("""
        SELECT cost_center_id, product_line, period_month, region,
               budget_quantity AS budget_qty,
               budget_amount_usd AS budget_amount
        FROM erp.budget
    """), engine)


# ------------------------------------------------------------------
# The decomposition — pure, no database
# ------------------------------------------------------------------

def decompose(df: pd.DataFrame, group_cols=None) -> pd.DataFrame:
    """
    Split actual-vs-budget into volume, mix and price at constant currency.

    Expects: group cols, product_line, actual_qty, actual_cc_amount,
             budget_qty, budget_amount.

    Mix is measured against the group's total volume, so it captures a shift in
    product composition independent of whether total volume moved at all. Volume
    is what remains once composition is held at plan.
    """
    group_cols = group_cols or GROUP_COLS
    d = df.copy()

    for col in ["actual_qty", "actual_cc_amount", "budget_qty", "budget_amount"]:
        d[col] = d[col].astype(float).fillna(0.0)

    d["budget_price"] = np.where(d["budget_qty"] > 0,
                                 d["budget_amount"] / d["budget_qty"], 0.0)
    d["actual_price_cc"] = np.where(d["actual_qty"] > 0,
                                    d["actual_cc_amount"] / d["actual_qty"], 0.0)

    g = d.groupby(group_cols)
    d["group_actual_qty"] = g["actual_qty"].transform("sum")
    d["group_budget_qty"] = g["budget_qty"].transform("sum")

    # units this product would have sold at plan composition and actual scale
    budget_share = np.where(d["group_budget_qty"] > 0,
                            d["budget_qty"] / d["group_budget_qty"], 0.0)
    expected_qty = d["group_actual_qty"] * budget_share

    d["volume_variance"] = (expected_qty - d["budget_qty"]) * d["budget_price"]
    d["mix_variance"] = (d["actual_qty"] - expected_qty) * d["budget_price"]
    d["price_variance"] = ((d["actual_price_cc"] - d["budget_price"])
                           * d["actual_qty"])

    # A product with no budget at all has no price basis to compare against,
    # so the whole amount is treated as volume rather than a price beat.
    unbudgeted = d["budget_qty"] <= 0
    d.loc[unbudgeted, "volume_variance"] = d.loc[unbudgeted, "actual_cc_amount"]
    d.loc[unbudgeted, "mix_variance"] = 0.0
    d.loc[unbudgeted, "price_variance"] = 0.0

    return d.drop(columns=["group_actual_qty", "group_budget_qty"])


def add_fx_effect(df: pd.DataFrame) -> pd.DataFrame:
    """Currency effect is reported actual minus constant-currency actual."""
    d = df.copy()
    d["fx_variance"] = (d["actual_amount"].astype(float)
                        - d["actual_cc_amount"].astype(float))
    d["total_variance"] = (d["actual_amount"].astype(float)
                           - d["budget_amount"].astype(float))
    return d


def assert_identity(df: pd.DataFrame, tolerance=IDENTITY_TOLERANCE) -> pd.DataFrame:
    """
    The four components must add back to the total. If they do not, the
    decomposition is wrong and every number downstream is untrustworthy.
    """
    d = df.copy()
    d["component_sum"] = (d["fx_variance"] + d["volume_variance"]
                          + d["mix_variance"] + d["price_variance"])
    d["identity_gap"] = d["total_variance"] - d["component_sum"]

    worst = d["identity_gap"].abs().max()
    if worst > tolerance:
        bad = d.loc[d["identity_gap"].abs() > tolerance].head()
        raise AssertionError(
            f"Variance identity broken: largest gap {worst:,.4f} USD "
            f"exceeds tolerance {tolerance}.\n{bad.to_string()}"
        )
    return d


# ------------------------------------------------------------------
# Orchestration
# ------------------------------------------------------------------

def run_variance(engine=None, period_month=None, region=None,
                 cost_center_id=None) -> dict:
    """
    Full variance analysis, optionally filtered.

    Returns the line-level detail, a bridge suitable for a waterfall, and a
    per-driver summary the commentary agent can quote.
    """
    engine = engine or get_engine()

    currency_map = load_currency_map(engine)
    actuals = load_actuals(engine, currency_map)
    budget = load_budget(engine)

    merged = budget.merge(
        actuals, on=["cost_center_id", "product_line", "period_month"],
        how="outer")
    merged["region"] = merged.groupby("cost_center_id")["region"].transform(
        lambda s: s.ffill().bfill())

    detail = assert_identity(add_fx_effect(decompose(merged)))

    if period_month is not None:
        detail = detail[detail["period_month"] == period_month]
    if region is not None:
        detail = detail[detail["region"] == region]
    if cost_center_id is not None:
        detail = detail[detail["cost_center_id"] == cost_center_id]

    bridge = {
        "budget": float(detail["budget_amount"].sum()),
        "volume": float(detail["volume_variance"].sum()),
        "mix": float(detail["mix_variance"].sum()),
        "price": float(detail["price_variance"].sum()),
        "fx": float(detail["fx_variance"].sum()),
        "actual": float(detail["actual_amount"].sum()),
    }
    bridge["total_variance"] = bridge["actual"] - bridge["budget"]
    bridge["variance_pct"] = (bridge["total_variance"] / bridge["budget"] * 100
                              if bridge["budget"] else 0.0)

    by_product = (detail.groupby("product_line", as_index=False)
                        .agg(budget_amount=("budget_amount", "sum"),
                             actual_amount=("actual_amount", "sum"),
                             volume=("volume_variance", "sum"),
                             mix=("mix_variance", "sum"),
                             price=("price_variance", "sum"),
                             fx=("fx_variance", "sum"),
                             total=("total_variance", "sum"))
                        .sort_values("total", key=abs, ascending=False))

    return {"detail": detail, "bridge": bridge, "by_product": by_product}


def top_variance_periods(engine=None, n=5) -> pd.DataFrame:
    """Cost centre / month combinations with the largest percentage gap."""
    result = run_variance(engine)
    d = result["detail"]
    agg = (d.groupby(["cost_center_id", "period_month"], as_index=False)
             .agg(budget=("budget_amount", "sum"),
                  actual=("actual_amount", "sum"),
                  volume=("volume_variance", "sum"),
                  mix=("mix_variance", "sum"),
                  price=("price_variance", "sum"),
                  fx=("fx_variance", "sum")))
    agg["total_variance"] = agg["actual"] - agg["budget"]
    agg["variance_pct"] = np.where(agg["budget"] != 0,
                                   agg["total_variance"] / agg["budget"] * 100, 0)
    return agg.reindex(agg["variance_pct"].abs().sort_values(
        ascending=False).index).head(n)


if __name__ == "__main__":
    eng = get_engine()
    res = run_variance(eng)
    b = res["bridge"]

    print("Full-period bridge (USD)")
    print(f"  Budget          {b['budget']:>16,.0f}")
    print(f"  Volume          {b['volume']:>16,.0f}")
    print(f"  Mix             {b['mix']:>16,.0f}")
    print(f"  Price           {b['price']:>16,.0f}")
    print(f"  FX              {b['fx']:>16,.0f}")
    print(f"  Actual          {b['actual']:>16,.0f}")
    print(f"  Total variance  {b['total_variance']:>16,.0f} "
          f"({b['variance_pct']:+.1f}%)")

    print("\nLargest variances by cost centre and month")
    print(top_variance_periods(eng).to_string(index=False))
