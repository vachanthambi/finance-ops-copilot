"""
Tools the agents are allowed to use.

Two kinds live here:

1. Read-only SQL, executed through a least-privilege login with a statement
   timeout and a row cap. The guard rejects anything that is not a single
   SELECT or WITH, so a bad generation cannot mutate the database. Privilege is
   the real protection; the guard is the cheap first line.

2. Thin wrappers over the Phase 2 engine. These do no arithmetic — they choose
   arguments, call the tested functions, and hand back the result. That is the
   whole point: the model decides *what* to compute, never *what the answer is*.
"""

import os
import re
from datetime import date

import pandas as pd
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

from engine.reconciliation import run_reconciliation
from engine.variance import run_variance, top_variance_periods

load_dotenv()

ROW_LIMIT = 200
STATEMENT_TIMEOUT_MS = 15000

FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|alter|create|truncate|grant|revoke|"
    r"copy|vacuum|reindex|call|do)\b", re.IGNORECASE)
COMMENTS = re.compile(r"(--[^\n]*)|(/\*.*?\*/)", re.DOTALL)

_ro_engine = None
_engine = None

# Reconciliation reloads every table and redoes entity resolution, so it is by
# far the most expensive call here. The underlying data cannot change during a
# request, so compute it once per process.
_recon_cache = None


def readonly_engine():
    """Least-privilege connection. Falls back to the app login if unconfigured."""
    global _ro_engine
    if _ro_engine is None:
        user = os.getenv("DB_RO_USER") or os.getenv("DB_USER")
        pwd = os.getenv("DB_RO_PASSWORD") or os.getenv("DB_PASSWORD")
        url = (f"postgresql+psycopg2://{user}:{pwd}@{os.getenv('DB_HOST')}"
               f":{os.getenv('DB_PORT')}/{os.getenv('DB_NAME')}")
        _ro_engine = create_engine(
            url, connect_args={"options": f"-c statement_timeout={STATEMENT_TIMEOUT_MS}"})
    return _ro_engine


def app_engine():
    global _engine
    if _engine is None:
        url = (f"postgresql+psycopg2://{os.getenv('DB_USER')}:{os.getenv('DB_PASSWORD')}"
               f"@{os.getenv('DB_HOST')}:{os.getenv('DB_PORT')}/{os.getenv('DB_NAME')}")
        _engine = create_engine(url)
    return _engine


# ------------------------------------------------------------------
# SQL guard
# ------------------------------------------------------------------

def validate_sql(sql: str) -> str:
    """Return cleaned SQL or raise. Rejects anything that is not a single read."""
    stripped = COMMENTS.sub(" ", sql).strip().rstrip(";").strip()
    if not stripped:
        raise ValueError("empty statement")
    if ";" in stripped:
        raise ValueError("multiple statements are not allowed")
    if not re.match(r"^\s*(select|with)\b", stripped, re.IGNORECASE):
        raise ValueError("only SELECT and WITH statements are permitted")
    hit = FORBIDDEN.search(stripped)
    if hit:
        raise ValueError(f"forbidden keyword '{hit.group(0)}'")
    return stripped


def apply_row_limit(sql: str, limit: int = ROW_LIMIT) -> str:
    if re.search(r"\blimit\s+\d+\s*$", sql, re.IGNORECASE):
        return sql
    return f"{sql}\nLIMIT {limit}"


# ------------------------------------------------------------------
# Tool handlers
# ------------------------------------------------------------------

def describe_schema(schema: str | None = None) -> dict:
    """Table and column listing so the agent can write valid SQL."""
    q = """
        SELECT table_schema, table_name, column_name, data_type
        FROM information_schema.columns
        WHERE table_schema IN ('crm', 'erp', 'billing')
        {filt}
        ORDER BY table_schema, table_name, ordinal_position
    """.format(filt="AND table_schema = :s" if schema else "")

    with readonly_engine().connect() as conn:
        rows = conn.execute(text(q), {"s": schema} if schema else {}).mappings().all()

    tables: dict = {}
    for r in rows:
        key = f"{r['table_schema']}.{r['table_name']}"
        tables.setdefault(key, []).append(f"{r['column_name']} {r['data_type']}")
    return {"tables": tables}


def execute_sql(sql: str, trace=None, agent: str = "retrieval") -> dict:
    """Run a validated read-only query and return rows plus the SQL actually run."""
    cleaned = apply_row_limit(validate_sql(sql))
    with readonly_engine().connect() as conn:
        df = pd.read_sql(text(cleaned), conn)

    if trace is not None:
        trace.add(agent, "sql", f"{len(df)} rows",
                  detail=cleaned[:400], sql=cleaned, row_count=len(df))

    return {
        "sql": cleaned,
        "row_count": int(len(df)),
        "truncated": len(df) >= ROW_LIMIT,
        "rows": df.head(ROW_LIMIT).to_dict(orient="records"),
    }


def reconcile(break_type: str | None = None, period_month: str | None = None,
              limit: int = 25) -> dict:
    """Run the deterministic reconciliation engine and summarise the breaks."""
    global _recon_cache
    if _recon_cache is None:
        _recon_cache = run_reconciliation(app_engine())
    result = _recon_cache
    breaks = result["breaks"]

    if break_type:
        breaks = breaks[breaks["break_type"] == break_type]
    if period_month:
        target = pd.to_datetime(period_month).date()
        breaks = breaks[breaks["period_month"] == target]

    summary = (breaks.groupby("break_type")
                     .agg(count=("break_type", "size"),
                          total_usd=("amount_usd", "sum"))
                     .reset_index() if not breaks.empty else pd.DataFrame())

    return {
        "transactions_reconciled": result["n_crm_transactions"],
        "erp_entries": result["n_erp_entries"],
        "invoices": result["n_invoices"],
        "summary": summary,
        "sample_breaks": breaks.head(limit),
        "total_breaks": int(len(breaks)),
    }


def variance(period_month: str | None = None, region: str | None = None,
             cost_center_id: int | None = None) -> dict:
    """Run the deterministic variance engine for an optional slice."""
    pm = pd.to_datetime(period_month).date() if period_month else None
    result = run_variance(app_engine(), period_month=pm, region=region,
                          cost_center_id=cost_center_id)
    return {
        "filters": {"period_month": period_month, "region": region,
                    "cost_center_id": cost_center_id},
        "bridge": result["bridge"],
        "by_product": result["by_product"],
    }


def worst_variances(n: int = 5, sort_by: str = "usd",
                    direction: str = "both") -> dict:
    """Rank cost centre / month combinations by variance size."""
    return {"periods": top_variance_periods(app_engine(), n=n,
                                            sort_by=sort_by,
                                            direction=direction)}


# ------------------------------------------------------------------
# Tool schemas
# ------------------------------------------------------------------

DESCRIBE_SCHEMA_TOOL = {
    "name": "describe_schema",
    "description": "List tables and columns in the crm, erp and billing schemas. "
                   "Call this before writing SQL rather than guessing column names.",
    "input_schema": {
        "type": "object",
        "properties": {
            "schema": {"type": "string", "enum": ["crm", "erp", "billing"],
                       "description": "Optional: restrict to one schema."}
        },
    },
}

EXECUTE_SQL_TOOL = {
    "name": "execute_sql",
    "description": (
        "Run a read-only SQL query against the source systems. SELECT and WITH "
        "only; a single statement; results are capped. Use this for facts the "
        "reconciliation and variance tools do not already provide."
    ),
    "input_schema": {
        "type": "object",
        "properties": {"sql": {"type": "string", "description": "PostgreSQL SELECT."}},
        "required": ["sql"],
    },
}

RECONCILE_TOOL = {
    "name": "reconcile",
    "description": (
        "Run the deterministic reconciliation engine across CRM, ERP and Billing. "
        "Returns counts and USD totals by break type: missing_in_erp, timing, "
        "fx_variance, duplicate_customer, unexplained. The numbers are computed "
        "in tested Python, not estimated."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "break_type": {"type": "string",
                           "enum": ["missing_in_erp", "timing", "fx_variance",
                                    "duplicate_customer", "unexplained"]},
            "period_month": {"type": "string",
                             "description": "First day of the month, YYYY-MM-DD."},
            "limit": {"type": "integer", "description": "Sample rows to return."},
        },
    },
}

VARIANCE_TOOL = {
    "name": "variance",
    "description": (
        "Run the deterministic variance engine. Returns a budget-to-actual bridge "
        "decomposed into volume, mix, price and FX, which sum exactly to the total "
        "variance, plus a per-product breakdown."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "period_month": {"type": "string",
                             "description": "First day of the month, YYYY-MM-DD."},
            "region": {"type": "string", "enum": ["NA", "EMEA", "APAC", "LATAM"]},
            "cost_center_id": {"type": "integer"},
        },
    },
}

WORST_VARIANCES_TOOL = {
    "name": "worst_variances",
    "description": (
        "Rank cost centre and month combinations by variance size. Set sort_by "
        "to 'usd' for absolute dollars or 'pct' for percentage of budget, and "
        "direction to 'unfavourable' for shortfalls or 'favourable' for beats. "
        "Use this when asked where the problem is, rather than checking a period "
        "the user already named."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "n": {"type": "integer", "description": "How many to return."},
            "sort_by": {"type": "string", "enum": ["usd", "pct"],
                        "description": "Rank by absolute dollars or by percentage."},
            "direction": {"type": "string",
                          "enum": ["unfavourable", "favourable", "both"],
                          "description": "Shortfalls, beats, or either."},
        },
    },
}