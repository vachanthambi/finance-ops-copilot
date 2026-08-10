"""
Finance Ops Copilot - Streamlit front end.

The audit panel is the point of this interface, not a debugging extra. A finance
team cannot act on a number it cannot trace, so every figure shown here can be
followed back to the deterministic engine call or SQL statement that produced
it, and the answer carries a badge saying whether it passed numeric validation
or fell back to raw engine output.

Run:  streamlit run app/main.py
"""

import os
import sys
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agents.base import Trace                       # noqa: E402
from agents.orchestrator import ask                 # noqa: E402
from engine.reconciliation import run_reconciliation  # noqa: E402
from engine.variance import get_engine, run_variance, top_variance_periods  # noqa: E402

st.set_page_config(page_title="Finance Ops Copilot", page_icon="§",
                   layout="wide")

BREAK_LABELS = {
    "missing_in_erp": "Closed in CRM, never posted to the ledger",
    "timing": "Invoiced in a different period from the ledger",
    "fx_variance": "Rate applied differs from the revenue month's rate",
    "duplicate_customer": "One entity held twice in CRM",
    "unexplained": "Matched but amounts disagree, cause not established",
}

EXAMPLES = [
    "Ranking by absolute USD, which cost centre and month had the largest "
    "unfavourable variance? Give the total and each driver in dollars.",
    "Across the entire dataset, give the count and total USD value of "
    "reconciliation breaks for every break type.",
    "How many transactions closed in CRM never reached the general ledger, "
    "and what is their total USD value?",
]


# ------------------------------------------------------------------
# Cached engine calls
# ------------------------------------------------------------------

@st.cache_data(show_spinner=False)
def load_variance():
    result = run_variance(get_engine())
    return result["bridge"], result["by_product"], result["detail"]


@st.cache_data(show_spinner=False)
def load_breaks():
    result = run_reconciliation(get_engine())
    return result["breaks"], result["summary"], result["n_crm_transactions"]


@st.cache_data(show_spinner=False)
def load_rankings():
    return (top_variance_periods(get_engine(), n=8, sort_by="usd",
                                 direction="unfavourable"),
            top_variance_periods(get_engine(), n=8, sort_by="usd",
                                 direction="favourable"))


# ------------------------------------------------------------------
# Charts
# ------------------------------------------------------------------

def waterfall(bridge: dict) -> go.Figure:
    """Budget to actual, walked one driver at a time."""
    fig = go.Figure(go.Waterfall(
        orientation="v",
        measure=["absolute", "relative", "relative", "relative", "relative",
                 "total"],
        x=["Budget", "Volume", "Mix", "Price", "FX", "Actual"],
        y=[bridge["budget"], bridge["volume"], bridge["mix"],
           bridge["price"], bridge["fx"], bridge["actual"]],
        text=[f"${v:,.0f}" for v in
              (bridge["budget"], bridge["volume"], bridge["mix"],
               bridge["price"], bridge["fx"], bridge["actual"])],
        textposition="outside",
        connector={"line": {"color": "#9aa0a6"}},
        increasing={"marker": {"color": "#2e7d32"}},
        decreasing={"marker": {"color": "#c62828"}},
        totals={"marker": {"color": "#37474f"}},
    ))
    fig.update_layout(height=420, margin=dict(t=30, b=20, l=10, r=10),
                      yaxis_title="USD", showlegend=False)
    return fig


# ------------------------------------------------------------------
# Audit panel
# ------------------------------------------------------------------

def render_trace(trace: Trace) -> None:
    st.caption(
        "Every model call, tool call and SQL statement behind the answer. "
        "The deterministic engine computes the figures; the agents choose what "
        "to compute and describe the result."
    )

    for i, step in enumerate(trace.steps, start=1):
        icon = {"model_call": "◇", "tool_call": "▣", "sql": "▤",
                "note": "·"}.get(step.kind, "·")
        timing = f" · {step.duration_ms} ms" if step.duration_ms else ""
        header = f"{icon}  {i}. [{step.agent}] {step.label}{timing}"

        with st.expander(header, expanded=False):
            if step.kind == "sql":
                st.code(step.payload.get("sql", step.detail), language="sql")
                st.caption(f"{step.payload.get('row_count', '?')} rows returned")
            elif step.detail:
                st.code(step.detail, language="json"
                        if step.kind == "tool_call" else None)
            if step.payload and step.kind == "model_call":
                st.caption(
                    f"in {step.payload.get('input_tokens', '?')} tokens · "
                    f"out {step.payload.get('output_tokens', '?')} tokens")


# ------------------------------------------------------------------
# Tabs
# ------------------------------------------------------------------

def tab_ask(persona: str) -> None:
    st.caption(
        "Ask about budget-to-actual performance or where the source systems "
        "disagree. Answers are written by an agent and every figure is checked "
        "against the engine before it is shown."
    )

    with st.form("ask"):
        question = st.text_area("Question", height=90,
                                placeholder=EXAMPLES[0])
        col_a, col_b = st.columns([1, 4])
        submitted = col_a.form_submit_button("Ask", type="primary")
        col_b.caption("Typically 30-60 seconds. Longer questions call more "
                      "specialists.")

    with st.expander("Example questions"):
        for ex in EXAMPLES:
            st.markdown(f"- {ex}")

    if not submitted:
        return
    if not question.strip():
        st.warning("Enter a question first.")
        return
    if not os.getenv("ANTHROPIC_API_KEY"):
        st.error("ANTHROPIC_API_KEY is not set. Add it to .env and restart.")
        return

    trace = Trace()
    with st.spinner("Routing to specialists..."):
        try:
            result = ask(question, persona=persona, trace=trace)
        except Exception as exc:
            st.error(f"{type(exc).__name__}: {exc}")
            return

    if result["validated"]:
        st.success("Every figure in this answer was verified against the "
                   "deterministic engine.")
    else:
        st.warning(
            "Generated commentary could not be verified, so the raw engine "
            "output is shown instead. This is the intended behaviour: an "
            "unpolished answer is preferable to an unchecked one."
        )

    # Streamlit parses $...$ as LaTeX, which swallows everything between any
    # two currency figures. Escaping keeps dollar amounts as dollar amounts.
    st.markdown(result["answer"].replace("$", r"\$"))

    a, b, c = st.columns(3)
    a.metric("Specialists used", len(result["agents_used"]))
    b.metric("SQL statements", len(result["sql_statements"]))
    c.metric("Trace steps", len(trace.steps))
    st.caption(" → ".join(result["agents_used"]))

    st.divider()
    st.subheader("Audit trail")
    render_trace(trace)

    with st.expander("Findings passed to the commentary agent"):
        st.text(result["findings"] or "(none)")


def tab_variance(persona: str) -> None:
    bridge, by_product, detail = load_variance()

    a, b, c, d = st.columns(4)
    a.metric("Budget", f"${bridge['budget']:,.0f}")
    b.metric("Actual", f"${bridge['actual']:,.0f}")
    c.metric("Variance", f"${bridge['total_variance']:,.0f}",
             f"{bridge['variance_pct']:+.1f}%")
    d.metric("FX effect", f"${bridge['fx']:,.0f}")

    st.plotly_chart(waterfall(bridge), use_container_width=True)

    if persona == "Finance Leader":
        st.caption(
            f"Operating drivers added ${bridge['volume'] + bridge['mix'] + bridge['price']:,.0f}; "
            f"currency movement contributed ${bridge['fx']:,.0f}. The four "
            "components sum exactly to the total variance - that identity is "
            "asserted in code, not assumed."
        )
    else:
        st.caption(
            "Volume and mix are operational levers; price and FX are commercial "
            "and treasury. Constant-currency restatement separates what the "
            "business did from what the exchange rate did."
        )

    st.subheader("By product line")
    st.dataframe(
        by_product.style.format({
            "budget_amount": "${:,.0f}", "actual_amount": "${:,.0f}",
            "volume": "${:,.0f}", "mix": "${:,.0f}", "price": "${:,.0f}",
            "fx": "${:,.0f}", "total": "${:,.0f}"}),
        use_container_width=True, hide_index=True)

    worst, best = load_rankings()
    left, right = st.columns(2)
    with left:
        st.subheader("Largest shortfalls")
        st.dataframe(worst[["cost_center_id", "period_month", "budget",
                            "actual", "total_variance", "variance_pct"]]
                     .style.format({"budget": "${:,.0f}", "actual": "${:,.0f}",
                                    "total_variance": "${:,.0f}",
                                    "variance_pct": "{:+.1f}%"}),
                     use_container_width=True, hide_index=True)
    with right:
        st.subheader("Largest beats")
        st.dataframe(best[["cost_center_id", "period_month", "budget",
                           "actual", "total_variance", "variance_pct"]]
                     .style.format({"budget": "${:,.0f}", "actual": "${:,.0f}",
                                    "total_variance": "${:,.0f}",
                                    "variance_pct": "{:+.1f}%"}),
                     use_container_width=True, hide_index=True)


def tab_reconciliation(persona: str) -> None:
    breaks, summary, n_txns = load_breaks()

    a, b, c = st.columns(3)
    a.metric("Transactions reconciled", f"{n_txns:,}")
    b.metric("Breaks found", f"{len(breaks):,}")
    unexplained = int((breaks["break_type"] == "unexplained").sum())
    c.metric("Unexplained", unexplained,
             help="Every real reconciliation leaves residue. Reporting it "
                  "honestly matters more than driving it to zero.")

    st.dataframe(
        summary.style.format({"total_usd": "${:,.2f}"}),
        use_container_width=True, hide_index=True)

    for bt, label in BREAK_LABELS.items():
        count = int((breaks["break_type"] == bt).sum())
        if count:
            st.caption(f"**{bt}** ({count}) — {label}")

    st.subheader("Break detail")
    types = sorted(breaks["break_type"].unique())
    chosen = st.multiselect("Filter by type", types, default=types)
    view = breaks[breaks["break_type"].isin(chosen)]

    if persona == "Ops Leader":
        st.caption(
            "Timing breaks are a process problem: the money is real and sits "
            "in the wrong period. Missing ledger entries are the ones that "
            "need chasing today."
        )

    st.dataframe(
        view[["break_type", "customer_name", "product_line", "period_month",
              "amount_usd", "detail"]]
        .style.format({"amount_usd": "${:,.2f}"}),
        use_container_width=True, hide_index=True, height=420)


def tab_about() -> None:
    st.markdown(
        """
### How it works

Three source systems share no join key: CRM uses `ACC-00123`, the ledger uses an
integer, billing uses an email address. Dates are text in three formats in one
system and proper dates in another. Identity is resolved by normalising company
names and fuzzy matching what remains.

**The deterministic engine runs first.** Reconciliation matching and variance
decomposition are plain tested Python — no model touches the arithmetic. Agents
call those functions as tools, choosing *what* to compute, never *what the
answer is*.

**Tool access is the guardrail, not the prompt.** Early versions gave the
reconciliation agent a SQL tool; it abandoned the tested engine and rewrote
matching by hand on `LOWER(customer_name)`, then reported figures the engine
never produced. Removing the tool fixed what three rounds of prompt rules could
not.

**Every figure is validated.** Generated commentary is parsed, and each number
checked against the engine's output. A failure triggers one retry; a second
failure falls back to displaying the raw findings. An unpolished answer is
always preferable to a fluent unchecked one.

### Measured

Across 12 runs of 6 questions on the final build: 9 answers passed numeric
validation with every figure traced to the engine, and 3 fell back to raw
output. No unverified figure reached the user in any run. Headline conclusions
agreed across repeat runs on half the questions, with 61% mean overlap in quoted
figures.

The engine is deterministic and reproduces exactly. The agent layer is not, and
no amount of prompting makes it so — which is precisely why the audit trail
exists.
        """
    )


# ------------------------------------------------------------------
# Main
# ------------------------------------------------------------------

def main() -> None:
    st.title("Finance Ops Copilot")
    st.caption("Multi-agent month-end close and variance analysis, with every "
               "figure traceable to a tested engine.")

    with st.sidebar:
        st.header("View")
        persona = st.radio(
            "Persona", ["Finance Leader", "Ops Leader"],
            help="Same figures, framed for a different reader. Finance sees "
                 "P&L consequence; Ops sees the process breakdown.")
        st.divider()
        st.caption("Sources: CRM · ERP · Billing")
        st.caption("24 months · ~2,900 transactions")
        if st.button("Clear cached engine results"):
            st.cache_data.clear()
            st.rerun()

    ask_tab, var_tab, rec_tab, about_tab = st.tabs(
        ["Ask", "Variance", "Reconciliation", "How it works"])

    with ask_tab:
        tab_ask(persona)
    with var_tab:
        tab_variance(persona)
    with rec_tab:
        tab_reconciliation(persona)
    with about_tab:
        tab_about()


if __name__ == "__main__":
    main()