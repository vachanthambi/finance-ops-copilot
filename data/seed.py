"""
Seed the three source systems with 24 months of transactions.

Approach: generate a canonical set of transactions first, then write each one
into CRM, ERP and Billing with system-specific distortions. Defects are created
by deliberately breaking that chain, and every affected ID is recorded in
data/defects_manifest.json — which becomes the Phase 2 test fixture.

Run:  python data/seed.py
"""

import json
import os
import random
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv
from faker import Faker
from sqlalchemy import create_engine, text

# ------------------------------------------------------------------
# Config
# ------------------------------------------------------------------

SEED = 42
N_ACCOUNTS = 60
N_MONTHS = 24
START_MONTH = date(2023, 1, 1)

DEFECT_COUNTS = {
    "missing_in_erp": 15,
    "timing": 30,
    "duplicate_customer": 8,
    "fx_variance": 12,
}
OVERRUN_MONTH_INDEX = 19          # month 20, zero-indexed
OVERRUN_MULTIPLIER = 1.40         # actuals land 40% over budget

REGIONS = ["NA", "EMEA", "APAC", "LATAM"]
REGION_CURRENCY = {
    "NA": ["USD"],
    "EMEA": ["EUR", "GBP"],
    "APAC": ["JPY", "AUD"],
    "LATAM": ["BRL", "MXN"],
}
BASE_FX = {
    "USD": 1.0, "EUR": 1.08, "GBP": 1.27,
    "JPY": 0.0067, "AUD": 0.66, "BRL": 0.20, "MXN": 0.058,
}
PRODUCT_LINES = [
    "Connectivity", "Managed Services", "Cloud Voice",
    "Security", "Professional Services",
]
INDUSTRIES = [
    "Manufacturing", "Financial Services", "Healthcare",
    "Retail", "Logistics", "Public Sector", "Technology",
]

random.seed(SEED)
fake = Faker()
Faker.seed(SEED)


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def month_list(start: date, n: int):
    months = []
    y, m = start.year, start.month
    for _ in range(n):
        months.append(date(y, m, 1))
        m += 1
        if m > 12:
            m = 1
            y += 1
    return months


def add_months(d: date, k: int) -> date:
    y, m = d.year, d.month + k
    y += (m - 1) // 12
    m = (m - 1) % 12 + 1
    return date(y, m, 1)


def random_day_in_month(month: date) -> date:
    nxt = add_months(month, 1)
    span = (nxt - month).days
    return month + timedelta(days=random.randint(0, span - 1))


def messy_date_string(d: date) -> str:
    """CRM stores dates as text in three inconsistent formats."""
    fmt = random.choice(["iso", "eu", "us_long"])
    if fmt == "iso":
        return d.strftime("%Y-%m-%d")
    if fmt == "eu":
        return d.strftime("%d/%m/%Y")
    return d.strftime("%b %d, %Y")


def name_variant(name: str) -> str:
    """Produce a plausible duplicate-record spelling of a company name."""
    swaps = [
        (" Limited", " Ltd"), (" Ltd", " Limited"),
        (" Incorporated", " Inc"), (" Inc", " Inc."),
        (" Corporation", " Corp"), (" Corp", " Corp."),
        (" and ", " & "), (" & ", " and "),
        (" Group", " Grp"), (" Company", " Co"),
    ]
    for old, new in swaps:
        if old in name:
            return name.replace(old, new)
    style = random.choice(["upper", "suffix", "punct"])
    if style == "upper":
        return name.upper()
    if style == "suffix":
        return name + " Ltd"
    return name.replace(",", "").replace(".", "")


def norm_email(company: str, idx: int) -> str:
    slug = "".join(ch for ch in company.lower() if ch.isalnum())[:18]
    return f"ap{idx}@{slug or 'client'}.com"


# ------------------------------------------------------------------
# Reference data
# ------------------------------------------------------------------

def build_cost_centers():
    rows, cid = [], 100
    for region in REGIONS:
        for func in ["Sales", "Delivery"]:
            rows.append({
                "cost_center_id": cid,
                "cost_center_code": f"CC-{region}-{func[:3].upper()}",
                "cost_center_name": f"{region} {func}",
                "region": region,
                "function": func,
            })
            cid += 1
    return pd.DataFrame(rows)


def build_accounts(cost_centers):
    sales_cc = {r.region: r.cost_center_id
                for r in cost_centers.itertuples() if r.function == "Sales"}
    accounts = []
    for i in range(1, N_ACCOUNTS + 1):
        region = random.choice(REGIONS)
        currency = random.choice(REGION_CURRENCY[region])
        company = fake.company()
        accounts.append({
            "account_id": f"ACC-{i:05d}",
            "customer_id": 5000 + i,
            "account_name": company,
            "email": norm_email(company, i),
            "region": region,
            "industry": random.choice(INDUSTRIES),
            "currency_code": currency,
            "owner_rep": fake.name(),
            "cost_center_id": sales_cc[region],
            "created_date": messy_date_string(
                date(2019, random.randint(1, 12), random.randint(1, 28))
            ),
        })
    return pd.DataFrame(accounts)


def build_fx(months):
    rows = []
    for m in months:
        for ccy, base in BASE_FX.items():
            drift = 1.0 if ccy == "USD" else random.uniform(0.94, 1.06)
            rows.append({
                "rate_month": m,
                "currency_code": ccy,
                "rate_to_usd": round(base * drift, 6),
            })
    return pd.DataFrame(rows)


# ------------------------------------------------------------------
# Canonical transactions
# ------------------------------------------------------------------

def build_transactions(accounts, months, fx):
    fx_lookup = {(r.rate_month, r.currency_code): float(r.rate_to_usd)
                 for r in fx.itertuples()}
    txns, uid = [], 1

    for month in months:
        for acct in accounts.itertuples():
            for product in random.sample(PRODUCT_LINES, random.randint(1, 3)):
                qty = random.randint(5, 400)
                unit_usd = round(random.uniform(45, 900), 4)
                amount_usd = round(qty * unit_usd, 2)
                rate = fx_lookup[(month, acct.currency_code)]
                amount_local = round(amount_usd / rate, 2)

                txns.append({
                    "txn_uid": uid,
                    "account_id": acct.account_id,
                    "customer_id": acct.customer_id,
                    "account_name": acct.account_name,
                    "email": acct.email,
                    "region": acct.region,
                    "cost_center_id": acct.cost_center_id,
                    "currency_code": acct.currency_code,
                    "period_month": month,
                    "txn_date": random_day_in_month(month),
                    "product_line": product,
                    "quantity": qty,
                    "unit_price_usd": unit_usd,
                    "amount_usd": amount_usd,
                    "unit_price_local": round(amount_local / qty, 2),
                    "amount_local": amount_local,
                    "fx_rate": rate,
                })
                uid += 1

    return pd.DataFrame(txns)


# ------------------------------------------------------------------
# Defects
# ------------------------------------------------------------------

def plant_defects(txns, accounts, months):
    manifest = {
        "seed": SEED,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "defects": {},
    }
    pool = txns[txns["period_month"] < months[-1]].copy()

    # 1. closed-won in CRM, never posted to the ledger
    missing = pool.sample(DEFECT_COUNTS["missing_in_erp"], random_state=SEED)
    missing_uids = set(missing["txn_uid"])
    manifest["defects"]["missing_in_erp"] = {
        "description": "CRM opportunity marked Closed Won with no corresponding ERP GL entry.",
        "count": len(missing_uids),
        "txn_uids": sorted(missing_uids),
    }

    # 2. billing recognises revenue one month later than the ledger
    remaining = pool[~pool["txn_uid"].isin(missing_uids)]
    timing = remaining.sample(DEFECT_COUNTS["timing"], random_state=SEED + 1)
    timing_uids = set(timing["txn_uid"])
    manifest["defects"]["timing"] = {
        "description": "Billing invoice dated one month after the ERP posting period.",
        "count": len(timing_uids),
        "txn_uids": sorted(timing_uids),
    }

    # 3. wrong month's FX rate applied at invoicing
    remaining = remaining[~remaining["txn_uid"].isin(timing_uids)]
    fxbad = remaining.sample(DEFECT_COUNTS["fx_variance"], random_state=SEED + 2)
    fx_uids = set(fxbad["txn_uid"])
    manifest["defects"]["fx_variance"] = {
        "description": "Billing applied the prior month's FX rate, so USD value diverges from ERP.",
        "count": len(fx_uids),
        "txn_uids": sorted(fx_uids),
    }

    # 4. duplicate CRM account records under name variants
    dup_accounts = accounts.sample(DEFECT_COUNTS["duplicate_customer"],
                                   random_state=SEED + 3)
    dup_map = {}
    for n, acct in enumerate(dup_accounts.itertuples(), start=1):
        dup_id = f"ACC-D{n:04d}"
        dup_map[acct.account_id] = {
            "duplicate_account_id": dup_id,
            "original_name": acct.account_name,
            "duplicate_name": name_variant(acct.account_name),
        }
    manifest["defects"]["duplicate_customer"] = {
        "description": "Same legal entity present twice in CRM under a spelling variant.",
        "count": len(dup_map),
        "pairs": dup_map,
    }

    # 5. genuine budget overrun in one cost centre
    overrun_month = months[OVERRUN_MONTH_INDEX]
    overrun_cc = int(accounts["cost_center_id"].mode().iloc[0])
    manifest["defects"]["budget_overrun"] = {
        "description": "Real operational overrun: actuals exceed budget by ~40%.",
        "cost_center_id": overrun_cc,
        "period_month": overrun_month.isoformat(),
        "multiplier": OVERRUN_MULTIPLIER,
    }

    return manifest, missing_uids, timing_uids, fx_uids, dup_map, overrun_cc, overrun_month


# ------------------------------------------------------------------
# System writers
# ------------------------------------------------------------------

def write_crm(txns, accounts, dup_map):
    acc_rows = [{
        "account_id": a.account_id,
        "account_name": a.account_name,
        "region": a.region,
        "industry": a.industry,
        "currency_code": a.currency_code,
        "owner_rep": a.owner_rep,
        "created_date": a.created_date,
    } for a in accounts.itertuples()]

    for orig_id, info in dup_map.items():
        src = accounts[accounts["account_id"] == orig_id].iloc[0]
        acc_rows.append({
            "account_id": info["duplicate_account_id"],
            "account_name": info["duplicate_name"],
            "region": src["region"],
            "industry": src["industry"],
            "currency_code": src["currency_code"],
            "owner_rep": fake.name(),
            "created_date": messy_date_string(date(2021, 6, 14)),
        })

    dup_targets = {k: v["duplicate_account_id"] for k, v in dup_map.items()}
    opp_rows = []
    for t in txns.itertuples():
        account_id = t.account_id
        # a slice of each duplicated account's deals sit on the duplicate record
        if account_id in dup_targets and t.txn_uid % 5 == 0:
            account_id = dup_targets[account_id]
        opp_rows.append({
            "opportunity_id": f"OPP-{t.txn_uid:07d}",
            "account_id": account_id,
            "product_line": t.product_line,
            "stage": "Closed Won",
            "close_date": messy_date_string(t.txn_date),
            "quantity": t.quantity,
            "unit_price_local": t.unit_price_local,
            "amount_local": t.amount_local,
            "currency_code": t.currency_code,
        })

    return pd.DataFrame(acc_rows), pd.DataFrame(opp_rows)


def write_erp(txns, accounts, cost_centers, missing_uids,
              overrun_cc, overrun_month):
    cust_rows = [{
        "customer_id": a.customer_id,
        "customer_name": a.account_name,
        "region": a.region,
        "cost_center_id": a.cost_center_id,
        "active_flag": True,
    } for a in accounts.itertuples()]

    gl_rows = []
    for t in txns.itertuples():
        if t.txn_uid in missing_uids:
            continue
        amount = float(t.amount_usd)
        qty = t.quantity
        if t.cost_center_id == overrun_cc and t.period_month == overrun_month:
            amount = round(amount * OVERRUN_MULTIPLIER, 2)
            qty = int(qty * OVERRUN_MULTIPLIER)
        gl_rows.append({
            "customer_id": t.customer_id,
            "cost_center_id": t.cost_center_id,
            "account_code": "4000",
            "account_name": "Product Revenue",
            "posting_date": t.txn_date,
            "period_month": t.period_month,
            "product_line": t.product_line,
            "quantity": qty,
            "unit_price_usd": t.unit_price_usd,
            "amount_usd": amount,
            "source_doc": f"OPP-{t.txn_uid:07d}",
        })

    return pd.DataFrame(cust_rows), pd.DataFrame(gl_rows)


def build_budget(gl, cost_centers, overrun_cc, overrun_month):
    grouped = (gl.groupby(["cost_center_id", "product_line", "period_month"],
                          as_index=False)
                 .agg(actual_qty=("quantity", "sum"),
                      actual_amt=("amount_usd", "sum")))
    cc_region = {r.cost_center_id: r.region for r in cost_centers.itertuples()}

    rows = []
    for g in grouped.itertuples():
        if g.cost_center_id == overrun_cc and g.period_month == overrun_month:
            divisor = OVERRUN_MULTIPLIER
        else:
            divisor = 1 + random.uniform(-0.08, 0.08)
        b_amt = round(float(g.actual_amt) / divisor, 2)
        b_qty = max(1, int(g.actual_qty / divisor))
        rows.append({
            "cost_center_id": g.cost_center_id,
            "product_line": g.product_line,
            "region": cc_region[g.cost_center_id],
            "period_month": g.period_month,
            "account_code": "4000",
            "budget_quantity": b_qty,
            "budget_unit_price_usd": round(b_amt / b_qty, 4),
            "budget_amount_usd": b_amt,
        })
    return pd.DataFrame(rows)


def write_billing(txns, fx, timing_uids, fx_uids, months):
    fx_lookup = {(r.rate_month, r.currency_code): float(r.rate_to_usd)
                 for r in fx.itertuples()}
    last_month = months[-1]

    inv_rows, line_rows = [], []
    for t in txns.itertuples():
        inv_month = t.period_month
        inv_date = t.txn_date
        if t.txn_uid in timing_uids and t.period_month < last_month:
            inv_month = add_months(t.period_month, 1)
            inv_date = random_day_in_month(inv_month)

        rate = float(t.fx_rate)
        if t.txn_uid in fx_uids:
            prior = add_months(t.period_month, -1)
            rate = fx_lookup.get((prior, t.currency_code), rate)

        amount_local = float(t.amount_local)
        invoice_id = f"INV-{t.period_month.year}-{t.txn_uid:07d}"

        inv_rows.append({
            "invoice_id": invoice_id,
            "customer_email": t.email,
            "customer_name_raw": t.account_name,
            "invoice_ts": datetime.combine(
                inv_date,
                datetime.min.time().replace(
                    hour=random.randint(8, 19), minute=random.randint(0, 59)
                ),
            ),
            "currency_code": t.currency_code,
            "fx_rate_applied": round(rate, 6),
            "total_amount_local": amount_local,
            "total_amount_usd": round(amount_local * rate, 2),
            "status": random.choices(["Paid", "Open", "Overdue"],
                                     weights=[0.8, 0.15, 0.05])[0],
        })
        line_rows.append({
            "invoice_id": invoice_id,
            "product_line": t.product_line,
            "quantity": t.quantity,
            "unit_price_local": t.unit_price_local,
            "line_amount_local": amount_local,
        })

    return pd.DataFrame(inv_rows), pd.DataFrame(line_rows)


# ------------------------------------------------------------------
# Main
# ------------------------------------------------------------------

def main():
    load_dotenv()
    url = (
        f"postgresql+psycopg2://{os.getenv('DB_USER')}:{os.getenv('DB_PASSWORD')}"
        f"@{os.getenv('DB_HOST')}:{os.getenv('DB_PORT')}/{os.getenv('DB_NAME')}"
    )
    engine = create_engine(url)

    root = Path(__file__).resolve().parent
    ddl = (root / "schema.sql").read_text(encoding="utf-8")

    print("Applying schema...")
    with engine.begin() as conn:
        conn.execute(text(ddl))

    print("Building reference data...")
    months = month_list(START_MONTH, N_MONTHS)
    cost_centers = build_cost_centers()
    accounts = build_accounts(cost_centers)
    fx = build_fx(months)

    print("Generating canonical transactions...")
    txns = build_transactions(accounts, months, fx)
    print(f"  {len(txns):,} transactions across {N_MONTHS} months")

    print("Planting defects...")
    (manifest, missing_uids, timing_uids, fx_uids,
     dup_map, overrun_cc, overrun_month) = plant_defects(txns, accounts, months)

    print("Writing CRM...")
    crm_accounts, crm_opps = write_crm(txns, accounts, dup_map)

    print("Writing ERP...")
    erp_customers, erp_gl = write_erp(
        txns, accounts, cost_centers, missing_uids, overrun_cc, overrun_month
    )
    erp_budget = build_budget(erp_gl, cost_centers, overrun_cc, overrun_month)

    print("Writing Billing...")
    bill_invoices, bill_lines = write_billing(txns, fx, timing_uids, fx_uids, months)

    loads = [
        (cost_centers, "cost_centers", "erp"),
        (erp_customers, "customers", "erp"),
        (crm_accounts, "accounts", "crm"),
        (crm_opps, "opportunities", "crm"),
        (erp_gl, "gl_entries", "erp"),
        (erp_budget, "budget", "erp"),
        (fx, "fx_rates", "billing"),
        (bill_invoices, "invoices", "billing"),
        (bill_lines, "invoice_lines", "billing"),
    ]
    for df, table, schema in loads:
        df.to_sql(table, engine, schema=schema, if_exists="append", index=False)
        print(f"  {schema}.{table}: {len(df):,} rows")

    manifest["row_counts"] = {f"{s}.{t}": len(d) for d, t, s in loads}
    manifest_path = root / "defects_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, default=str),
                             encoding="utf-8")

    print(f"\nManifest written to {manifest_path}")
    print("Seed complete.")


if __name__ == "__main__":
    main()
