"""
Deterministic reconciliation across CRM, ERP and Billing.

No LLM anywhere in this module. Given the same database it returns the same
answer every time, which is what makes the agent layer in Phase 3 trustworthy —
the model narrates these numbers, it never computes them.

Matching rule
-------------
A transaction is identified by (resolved customer, product line, month).
CRM and ERP must agree on the month; Billing is allowed to sit one month either
side, because late invoicing is a known and legitimate pattern.

Deliberately NOT used: erp.gl_entries.source_doc. It carries the originating
opportunity id, which would reduce this to a join. Real source systems have no
such key — that absence is the entire reason reconciliation is a job.

Break precedence
----------------
duplicate_customer -> timing -> fx_variance -> missing_in_erp -> unexplained
"""

import os
from dataclasses import dataclass

import pandas as pd
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

from engine.entity_resolution import build_crosswalk
from engine.normalize import month_diff, month_floor, parse_messy_date

AMOUNT_TOLERANCE = 0.005   # 0.5% — absorbs rounding, not real differences
FX_TOLERANCE = 1e-6


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


@dataclass
class SourceData:
    crm_accounts: pd.DataFrame
    crm_opps: pd.DataFrame
    erp_customers: pd.DataFrame
    erp_gl: pd.DataFrame
    billing_invoices: pd.DataFrame
    fx_rates: pd.DataFrame


def load_sources(engine) -> SourceData:
    q = lambda sql: pd.read_sql(text(sql), engine)
    return SourceData(
        crm_accounts=q("SELECT * FROM crm.accounts"),
        crm_opps=q("SELECT * FROM crm.opportunities"),
        erp_customers=q("SELECT * FROM erp.customers"),
        erp_gl=q("""SELECT entry_id, customer_id, cost_center_id, period_month,
                           posting_date, product_line, quantity,
                           unit_price_usd, amount_usd
                    FROM erp.gl_entries"""),
        billing_invoices=q("""SELECT invoice_id, customer_email, customer_name_raw,
                                     invoice_ts, currency_code, fx_rate_applied,
                                     total_amount_local, total_amount_usd, status
                              FROM billing.invoices"""),
        fx_rates=q("SELECT * FROM billing.fx_rates"),
    )


# ------------------------------------------------------------------
# Preparation
# ------------------------------------------------------------------

def prepare_crm(src: SourceData, crm_map: dict) -> pd.DataFrame:
    """Parse the text dates, resolve identity, and convert local amounts to USD."""
    opps = src.crm_opps.merge(
        src.crm_accounts[["account_id", "currency_code"]],
        on="account_id", how="left", suffixes=("", "_acct"),
    )
    opps["close_dt"] = opps["close_date"].map(parse_messy_date)
    opps["period_month"] = opps["close_dt"].map(month_floor)
    opps["customer_id"] = opps["account_id"].map(crm_map)

    fx = src.fx_rates.rename(columns={"rate_month": "period_month"})
    opps = opps.merge(fx, on=["period_month", "currency_code"], how="left")
    opps["amount_usd_crm"] = (opps["amount_local"].astype(float)
                              * opps["rate_to_usd"].astype(float)).round(2)

    return opps[["opportunity_id", "account_id", "customer_id", "product_line",
                 "period_month", "currency_code", "quantity", "amount_local",
                 "amount_usd_crm", "rate_to_usd"]]


def prepare_billing(src: SourceData, email_map: dict) -> pd.DataFrame:
    inv = src.billing_invoices.copy()
    inv["invoice_month"] = pd.to_datetime(inv["invoice_ts"]).dt.date.map(month_floor)
    inv["customer_id"] = inv["customer_email"].map(email_map)

    lines = pd.read_sql(
        text("SELECT invoice_id, product_line, quantity, line_amount_local "
             "FROM billing.invoice_lines"),
        get_engine(),
    )
    return inv.merge(lines, on="invoice_id", how="left")


# ------------------------------------------------------------------
# Matching
# ------------------------------------------------------------------

def _amounts_agree(a, b, tol=AMOUNT_TOLERANCE) -> bool:
    a, b = float(a), float(b)
    if a == 0 and b == 0:
        return True
    return abs(a - b) / max(abs(a), abs(b)) <= tol


def match_crm_to_erp(crm: pd.DataFrame, gl: pd.DataFrame) -> pd.DataFrame:
    """
    Exact-month join on (customer, product line, month).

    The seed guarantees at most one transaction per customer/product/month, so
    this is a clean one-to-one merge rather than a search.
    """
    gl_agg = (gl.groupby(["customer_id", "product_line", "period_month"],
                         as_index=False)
                .agg(erp_amount_usd=("amount_usd", "sum"),
                     erp_quantity=("quantity", "sum"),
                     erp_entries=("entry_id", "count")))

    merged = crm.merge(gl_agg, on=["customer_id", "product_line", "period_month"],
                       how="left")
    merged["erp_matched"] = merged["erp_amount_usd"].notna()
    merged["amount_agrees"] = merged.apply(
        lambda r: _amounts_agree(r["amount_usd_crm"], r["erp_amount_usd"])
        if pd.notna(r["erp_amount_usd"]) else False, axis=1)
    return merged


def match_crm_to_billing(crm: pd.DataFrame, billing: pd.DataFrame) -> pd.DataFrame:
    """
    Join on (customer, product line) then filter to a +/- 1 month window with
    agreeing local amounts. The window is what lets a late invoice resolve as a
    timing difference instead of a missing record plus an unexplained extra.

    Matching is one-to-one: an invoice settles exactly one opportunity. Without
    that constraint two opportunities can claim the same invoice, and the loser
    silently pairs with a neighbouring month — which then looks like an FX or
    timing break that never happened.
    """
    b = billing[["invoice_id", "customer_id", "product_line", "invoice_month",
                 "currency_code", "fx_rate_applied", "total_amount_local",
                 "total_amount_usd"]].copy()

    pairs = crm[["opportunity_id", "customer_id", "product_line", "period_month",
                 "amount_local"]].merge(
        b, on=["customer_id", "product_line"], how="left", suffixes=("", "_bill"))

    pairs = pairs[pairs["invoice_id"].notna()].copy()
    pairs["month_gap"] = pairs.apply(
        lambda r: month_diff(r["period_month"], r["invoice_month"]), axis=1)
    pairs = pairs[pairs["month_gap"].abs() <= 1]
    pairs = pairs[pairs.apply(
        lambda r: _amounts_agree(r["amount_local"], r["total_amount_local"]),
        axis=1)].copy()

    # Greedy assignment: closest amount wins, month gap breaks ties.
    pairs["amount_gap"] = (pairs["amount_local"].astype(float)
                           - pairs["total_amount_local"].astype(float)).abs()
    pairs["gap_abs"] = pairs["month_gap"].abs()
    pairs = pairs.sort_values(["amount_gap", "gap_abs"])
    pairs = pairs.drop_duplicates("opportunity_id", keep="first")
    pairs = pairs.drop_duplicates("invoice_id", keep="first")
    return pairs


# ------------------------------------------------------------------
# Classification
# ------------------------------------------------------------------

def classify_breaks(crm_erp: pd.DataFrame, crm_bill: pd.DataFrame,
                    fx: pd.DataFrame, duplicates: pd.DataFrame,
                    names: dict) -> pd.DataFrame:
    breaks = []

    # 1. duplicate customers — identity problems come first
    for r in duplicates.itertuples():
        breaks.append({
            "break_type": "duplicate_customer",
            "customer_id": r.customer_id,
            "customer_name": names.get(r.customer_id, ""),
            "product_line": None,
            "period_month": None,
            "amount_usd": None,
            "detail": f"CRM holds {r.n_records} records for this entity: "
                      f"{', '.join(r.crm_account_ids)} "
                      f"({' / '.join(r.crm_account_names)})",
        })

    bill_by_opp = crm_bill.set_index("opportunity_id")
    fx_lookup = {(r.rate_month, r.currency_code): float(r.rate_to_usd)
                 for r in fx.itertuples()}

    for r in crm_erp.itertuples():
        opp = r.opportunity_id
        bill = bill_by_opp.loc[opp] if opp in bill_by_opp.index else None

        # 2. timing — billed in a different period from the ledger
        if bill is not None and bill["month_gap"] != 0:
            breaks.append({
                "break_type": "timing",
                "customer_id": r.customer_id,
                "customer_name": names.get(r.customer_id, ""),
                "product_line": r.product_line,
                "period_month": r.period_month,
                "amount_usd": r.amount_usd_crm,
                "detail": f"Invoice {bill['invoice_id']} dated "
                          f"{bill['invoice_month']}, revenue period "
                          f"{r.period_month} ({int(bill['month_gap']):+d} month)",
            })
            continue

        # 3. fx variance — rate applied is not the revenue month's rate
        if bill is not None:
            true_rate = fx_lookup.get((r.period_month, bill["currency_code"]))
            applied = float(bill["fx_rate_applied"])
            if true_rate is not None and abs(applied - true_rate) > FX_TOLERANCE:
                impact = round(float(bill["total_amount_local"])
                               * (applied - true_rate), 2)
                breaks.append({
                    "break_type": "fx_variance",
                    "customer_id": r.customer_id,
                    "customer_name": names.get(r.customer_id, ""),
                    "product_line": r.product_line,
                    "period_month": r.period_month,
                    "amount_usd": impact,
                    "detail": f"Invoice {bill['invoice_id']} applied "
                              f"{applied:.6f} against a period rate of "
                              f"{true_rate:.6f}; USD impact {impact:,.2f}",
                })
                continue

        # 4. closed in CRM, never posted to the ledger
        if not r.erp_matched:
            breaks.append({
                "break_type": "missing_in_erp",
                "customer_id": r.customer_id,
                "customer_name": names.get(r.customer_id, ""),
                "product_line": r.product_line,
                "period_month": r.period_month,
                "amount_usd": r.amount_usd_crm,
                "detail": f"Opportunity {opp} closed won, no GL entry in "
                          f"{r.period_month}",
            })
            continue

        # 5. matched but the numbers disagree
        if not r.amount_agrees:
            breaks.append({
                "break_type": "unexplained",
                "customer_id": r.customer_id,
                "customer_name": names.get(r.customer_id, ""),
                "product_line": r.product_line,
                "period_month": r.period_month,
                "amount_usd": round(float(r.amount_usd_crm)
                                    - float(r.erp_amount_usd), 2),
                "detail": f"CRM {r.amount_usd_crm:,.2f} USD vs ERP "
                          f"{r.erp_amount_usd:,.2f} USD",
            })

    return pd.DataFrame(breaks)


# ------------------------------------------------------------------
# Entry point
# ------------------------------------------------------------------

def run_reconciliation(engine=None) -> dict:
    """
    Full reconciliation. Returns the break table plus a summary the agent layer
    can quote directly.
    """
    engine = engine or get_engine()
    src = load_sources(engine)

    xwalk = build_crosswalk(src.crm_accounts, src.erp_customers,
                            src.billing_invoices)
    names = dict(zip(src.erp_customers["customer_id"],
                     src.erp_customers["customer_name"]))

    crm = prepare_crm(src, xwalk.crm_map())
    billing = prepare_billing(src, xwalk.email_map())

    crm_erp = match_crm_to_erp(crm, src.erp_gl)
    crm_bill = match_crm_to_billing(crm, billing)
    breaks = classify_breaks(crm_erp, crm_bill, src.fx_rates,
                             xwalk.duplicates, names)

    summary = (breaks.groupby("break_type")
                     .agg(count=("break_type", "size"),
                          total_usd=("amount_usd", "sum"))
                     .reset_index()
               if not breaks.empty else pd.DataFrame())

    return {
        "breaks": breaks,
        "summary": summary,
        "crosswalk": xwalk,
        "n_crm_transactions": len(crm),
        "n_erp_entries": len(src.erp_gl),
        "n_invoices": len(src.billing_invoices),
    }


if __name__ == "__main__":
    result = run_reconciliation()
    print(f"CRM transactions : {result['n_crm_transactions']:,}")
    print(f"ERP entries      : {result['n_erp_entries']:,}")
    print(f"Invoices         : {result['n_invoices']:,}")
    print("\nBreaks by type")
    print(result["summary"].to_string(index=False))
