"""
Link customer records across CRM, ERP and Billing.

The three systems share no key: CRM uses 'ACC-00123', ERP an integer, Billing
an email address. The only common signal is the company name, and it is spelled
inconsistently. So we normalise hard, match exactly where we can, and fall back
to fuzzy matching for the rest.

ERP is treated as the master, because the general ledger is the system of record.
"""

from dataclasses import dataclass, field

import pandas as pd
from rapidfuzz import fuzz, process

from engine.normalize import normalize_company_name

# Tuned against the eight known duplicate pairs in the seed manifest.
# Lower and unrelated companies start merging; higher and spelling variants
# such as "Acme" vs "Acme Ltd" are missed.
FUZZY_THRESHOLD = 90


@dataclass
class Crosswalk:
    """Resolved identity map plus the duplicate pairs found along the way."""
    crm_to_erp: pd.DataFrame          # account_id -> customer_id
    email_to_erp: pd.DataFrame        # customer_email -> customer_id
    duplicates: pd.DataFrame = field(default_factory=pd.DataFrame)

    def crm_map(self) -> dict:
        return dict(zip(self.crm_to_erp["account_id"],
                        self.crm_to_erp["customer_id"]))

    def email_map(self) -> dict:
        return dict(zip(self.email_to_erp["customer_email"],
                        self.email_to_erp["customer_id"]))


def _match_names(candidates: pd.DataFrame, master: pd.DataFrame,
                 cand_key: str, cand_name: str) -> pd.DataFrame:
    """
    Match candidate names to the ERP master.

    Exact match on the normalised name first (cheap, unambiguous), then fuzzy
    matching on whatever is left over.
    """
    master = master.copy()
    master["norm"] = master["customer_name"].map(normalize_company_name)
    lookup = dict(zip(master["norm"], master["customer_id"]))
    choices = list(lookup.keys())

    cand = candidates.copy()
    cand["norm"] = cand[cand_name].map(normalize_company_name)

    rows = []
    for r in cand.itertuples():
        norm = getattr(r, "norm")
        key = getattr(r, cand_key)

        if norm in lookup:
            rows.append({cand_key: key, "customer_id": lookup[norm],
                         "match_method": "exact", "match_score": 100.0})
            continue

        hit = process.extractOne(norm, choices, scorer=fuzz.token_set_ratio)
        if hit and hit[1] >= FUZZY_THRESHOLD:
            rows.append({cand_key: key, "customer_id": lookup[hit[0]],
                         "match_method": "fuzzy", "match_score": float(hit[1])})
        else:
            rows.append({cand_key: key, "customer_id": None,
                         "match_method": "unmatched", "match_score": 0.0})

    return pd.DataFrame(rows)


def build_crosswalk(crm_accounts: pd.DataFrame,
                    erp_customers: pd.DataFrame,
                    billing_invoices: pd.DataFrame) -> Crosswalk:
    """
    Produce the identity map and flag CRM duplicates.

    A duplicate is two distinct CRM account_ids resolving to the same ERP
    customer — the same legal entity entered twice under a spelling variant.
    """
    crm_to_erp = _match_names(
        crm_accounts[["account_id", "account_name"]],
        erp_customers[["customer_id", "customer_name"]],
        cand_key="account_id", cand_name="account_name",
    ).merge(crm_accounts[["account_id", "account_name"]], on="account_id")

    emails = (billing_invoices[["customer_email", "customer_name_raw"]]
              .drop_duplicates("customer_email"))
    email_to_erp = _match_names(
        emails, erp_customers[["customer_id", "customer_name"]],
        cand_key="customer_email", cand_name="customer_name_raw",
    )

    matched = crm_to_erp[crm_to_erp["customer_id"].notna()]
    counts = matched.groupby("customer_id")["account_id"].count()
    dup_ids = counts[counts > 1].index

    dup_rows = []
    for cid in dup_ids:
        group = matched[matched["customer_id"] == cid].sort_values("account_id")
        names = list(group["account_name"])
        ids = list(group["account_id"])
        dup_rows.append({
            "customer_id": cid,
            "crm_account_ids": ids,
            "crm_account_names": names,
            "n_records": len(ids),
        })

    return Crosswalk(
        crm_to_erp=crm_to_erp,
        email_to_erp=email_to_erp,
        duplicates=pd.DataFrame(dup_rows),
    )
