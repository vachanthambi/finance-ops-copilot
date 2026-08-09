"""
Confirm the seeded database matches the defect manifest.

This is a sanity check on the data, not a test of the reconciliation engine —
that comes in Phase 2. Here we only assert the defects were actually planted.

Run:  python data/verify_seed.py
"""

import json
import os
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv
from sqlalchemy import create_engine, text


def check(label, passed, detail=""):
    mark = "PASS" if passed else "FAIL"
    print(f"[{mark}] {label}{(' — ' + detail) if detail else ''}")
    return passed


def main():
    load_dotenv()
    url = (
        f"postgresql+psycopg2://{os.getenv('DB_USER')}:{os.getenv('DB_PASSWORD')}"
        f"@{os.getenv('DB_HOST')}:{os.getenv('DB_PORT')}/{os.getenv('DB_NAME')}"
    )
    engine = create_engine(url)

    root = Path(__file__).resolve().parent
    manifest = json.loads((root / "defects_manifest.json").read_text(encoding="utf-8"))
    d = manifest["defects"]
    results = []

    with engine.connect() as conn:
        # row counts
        for tbl, expected in manifest["row_counts"].items():
            schema, name = tbl.split(".")
            actual = conn.execute(
                text(f"SELECT COUNT(*) FROM {schema}.{name}")
            ).scalar()
            results.append(check(f"row count {tbl}", actual == expected,
                                 f"{actual:,} rows"))

        # 1. missing in ERP — opportunities with no matching source_doc
        missing = conn.execute(text("""
            SELECT COUNT(*)
            FROM crm.opportunities o
            LEFT JOIN erp.gl_entries g ON g.source_doc = o.opportunity_id
            WHERE g.entry_id IS NULL
        """)).scalar()
        results.append(check("missing_in_erp count",
                             missing == d["missing_in_erp"]["count"],
                             f"found {missing}"))

        # 2. timing — invoice month later than GL period
        timing = conn.execute(text("""
            SELECT COUNT(*)
            FROM erp.gl_entries g
            JOIN billing.invoices i
              ON i.invoice_id = 'INV-' || EXTRACT(YEAR FROM g.period_month)::int
                 || '-' || LPAD(SPLIT_PART(g.source_doc, '-', 2), 7, '0')
            WHERE DATE_TRUNC('month', i.invoice_ts)::date > g.period_month
        """)).scalar()
        results.append(check("timing count",
                             timing == d["timing"]["count"],
                             f"found {timing}"))

        # 3. duplicate CRM accounts
        dups = conn.execute(text("""
            SELECT COUNT(*) FROM crm.accounts WHERE account_id LIKE 'ACC-D%'
        """)).scalar()
        results.append(check("duplicate_customer count",
                             dups == d["duplicate_customer"]["count"],
                             f"found {dups}"))

        # 4. FX variance — applied rate differs from that month's rate
        fxbad = conn.execute(text("""
            SELECT COUNT(*)
            FROM billing.invoices i
            JOIN billing.fx_rates r
              ON r.rate_month = DATE_TRUNC('month', i.invoice_ts)::date
             AND r.currency_code = i.currency_code
            WHERE ABS(i.fx_rate_applied - r.rate_to_usd) > 0.000001
        """)).scalar()
        results.append(check("fx_variance detectable", fxbad > 0,
                             f"{fxbad} invoices with off-period rate "
                             "(includes timing-shifted invoices)"))

        # 5. budget overrun
        ov = d["budget_overrun"]
        row = conn.execute(text("""
            SELECT COALESCE(SUM(g.amount_usd), 0) AS actual,
                   (SELECT COALESCE(SUM(b.budget_amount_usd), 0)
                      FROM erp.budget b
                     WHERE b.cost_center_id = :cc
                       AND b.period_month = :pm) AS budget
            FROM erp.gl_entries g
            WHERE g.cost_center_id = :cc AND g.period_month = :pm
        """), {"cc": ov["cost_center_id"], "pm": ov["period_month"]}).mappings().one()
        actual, budget = float(row["actual"]), float(row["budget"])
        pct = (actual / budget - 1) * 100 if budget else 0
        results.append(check("budget overrun ~40%", 35 <= pct <= 45,
                             f"actual {actual:,.0f} vs budget {budget:,.0f} "
                             f"= {pct:+.1f}%"))

        # 6. messy CRM dates present in all three formats
        fmts = conn.execute(text("""
            SELECT
              SUM(CASE WHEN close_date ~ '^\\d{4}-\\d{2}-\\d{2}$' THEN 1 ELSE 0 END) AS iso,
              SUM(CASE WHEN close_date ~ '^\\d{2}/\\d{2}/\\d{4}$' THEN 1 ELSE 0 END) AS eu,
              SUM(CASE WHEN close_date ~ '^[A-Za-z]{3} \\d{2}, \\d{4}$' THEN 1 ELSE 0 END) AS us
            FROM crm.opportunities
        """)).mappings().one()
        results.append(check("three date formats present",
                             all(v > 0 for v in fmts.values()),
                             f"iso={fmts['iso']}, eu={fmts['eu']}, us={fmts['us']}"))

    print()
    if all(results):
        print("All checks passed. Phase 1 complete.")
    else:
        print(f"{sum(1 for r in results if not r)} check(s) failed.")


if __name__ == "__main__":
    main()
