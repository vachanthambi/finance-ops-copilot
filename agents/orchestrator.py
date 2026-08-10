"""
The orchestrator.

It plans, routes to specialists, and calls commentary last. It has no database
access and no engine access of its own — every fact in the final answer arrived
through a specialist, which in turn got it from the tested Phase 2 code.

Run:  python -m agents.orchestrator "why did EMEA miss plan in August 2024?"
"""

import sys

from agents.base import AgentResult, Trace, run_agent
from agents.subagents import (commentary_agent, reconciliation_agent,
                              retrieval_agent, variance_agent)
from agents.validator import (fallback_summary, validate_commentary)

ORCHESTRATOR_SYSTEM = """
You coordinate a finance operations team. You do not query data or calculate
anything yourself; you decide who to ask and in what order.

Your specialists:

  analyse_variance        budget versus actual, decomposed into volume, mix,
                          price and FX. Use for "why did we miss/beat plan",
                          "what drove the gap", "where is the problem".
  analyse_reconciliation  disagreements between CRM, ERP and Billing. Use for
                          "do the systems agree", "what is unmatched", "is
                          revenue missing", "why does billing differ".
  query_data              anything else factual: counts, rankings, lookups,
                          customer or product detail. Never budget-versus-actual.
  draft_commentary        the final written answer. Always call this last.

Any question comparing actual to budget, plan or forecast goes to
analyse_variance, however it is phrased - including "largest variance", "biggest
gap", "who missed", "who beat", "where did we overspend". Never send those to
query_data: it returns a bare total with no drivers attached, which is not an
answer to a variance question.

How to work:

1. Plan first. State in one or two sentences which specialists you need and why.
2. Call them. A question about a shortfall usually needs variance for the size
   and driver, then reconciliation to check the gap is real rather than a data
   problem — an apparent miss caused by unposted revenue is not a trading miss.
3. Call draft_commentary exactly once, last, passing the persona and the
   verified figures you collected. Do not paraphrase the numbers on the way in.
   Pass variance figures and reconciliation figures as separate labelled
   sections. Do not editorialise, and do not assert that one explains the other
   unless the breaks are in the same period and cost centre and are smaller in
   magnitude than the variance.
4. Return the commentary as your final answer. Add nothing to it.

draft_commentary is not optional. Every answer goes through it, including short
factual ones. Never write the final answer yourself: your text is not validated
against the findings, so nothing you write directly can be trusted.

Pass every figure as an absolute amount. Never compute or include a driver's
share of the total variance as a percentage. The components are signed, so
unsigned shares of them do not sum to 100 and are misleading. "Volume -$156,036
of a -$170,897 total" is correct; "volume is 91% of the variance" is not.

Only call query_data when the answer needs a fact the specialists did not
provide and that will appear in your final answer. Looking up which cost centre
a customer belongs to, when the answer is about a cost centre you already
identified, is wasted work.

Do not use query_data to investigate reconciliation or variance. If the
reconciliation specialist reports no breaks, that is the finding - accept it and
move on. Chasing it further with ad-hoc SQL reproduces the tested engine badly
and wastes the run.

Reconciliation and variance answer different questions. A variance is a real
performance gap unless the reconciliation specialist reports breaks that explain
it. Never describe a miss as presentational, or as a timing or billing issue,
unless reconciliation returned timing breaks of comparable size in that period.
If reconciliation reports no material breaks, the variance stands as genuine
performance and you must say so.

If a specialist returns a response beginning with INCOMPLETE, it ran out of
turns and its findings are unreliable. Do not use any figure from it. Either ask
that specialist a narrower, more specific question, or state plainly in your
answer that the analysis could not be completed. Never paper over it.

Never state a figure that a specialist did not give you.
""".strip()

TOOLS = [
    {
        "name": "analyse_variance",
        "description": "Ask the variance specialist about budget-to-actual "
                       "performance and its drivers.",
        "input_schema": {
            "type": "object",
            "properties": {"question": {"type": "string",
                                        "description": "A specific question, "
                                        "naming period and region if known."}},
            "required": ["question"],
        },
    },
    {
        "name": "analyse_reconciliation",
        "description": "Ask the reconciliation specialist where the three "
                       "source systems disagree.",
        "input_schema": {
            "type": "object",
            "properties": {"question": {"type": "string"}},
            "required": ["question"],
        },
    },
    {
        "name": "query_data",
        "description": "Ask the retrieval specialist a factual question "
                       "answerable with SQL: counts, rankings, lookups, "
                       "customer or product detail. NOT for budget-versus-"
                       "actual questions - those go to analyse_variance.",
        "input_schema": {
            "type": "object",
            "properties": {"question": {"type": "string"}},
            "required": ["question"],
        },
    },
    {
        "name": "draft_commentary",
        "description": "Hand verified findings to the commentary writer. Call "
                       "this exactly once, as the final step.",
        "input_schema": {
            "type": "object",
            "properties": {
                "findings": {"type": "string",
                             "description": "The figures gathered, verbatim."},
                "persona": {"type": "string",
                            "enum": ["Finance Leader", "Ops Leader"]},
            },
            "required": ["findings", "persona"],
        },
    },
]


def _validated_commentary(persona: str, question: str, findings: str,
                          trace: Trace) -> dict:
    """
    Draft commentary, then check every figure in it against the findings.

    One retry naming the offending figures, then a fallback that simply shows
    the verified numbers. A less polished answer is always preferable to a
    fluent one containing a figure the engine never produced.
    """
    result = commentary_agent(persona, question, findings, trace)
    check = validate_commentary(result.text, findings)
    trace.add("validator", "note",
              "passed" if check.ok else "failed",
              detail=check.message() or f"{check.checked} figures verified",
              checked=check.checked, ok=check.ok)

    if check.ok:
        return {"commentary": result.text, "validated": True}

    retry_note = (
        f"{findings}\n\n"
        f"IMPORTANT: a previous draft was rejected because it contained "
        f"{check.message()}. Use only the figures above, as absolute amounts. "
        f"Do not express any component as a percentage."
    )
    result = commentary_agent(persona, question, retry_note, trace)
    check = validate_commentary(result.text, findings)
    trace.add("validator", "note",
              "passed on retry" if check.ok else "failed on retry",
              detail=check.message() or f"{check.checked} figures verified",
              checked=check.checked, ok=check.ok)

    if check.ok:
        return {"commentary": result.text, "validated": True, "retried": True}

    return {"commentary": fallback_summary(findings, persona),
            "validated": False, "fell_back": True}


def ask(question: str, persona: str = "Finance Leader",
        trace: Trace | None = None) -> dict:
    """Answer a finance operations question end to end."""
    trace = trace or Trace()
    trace.add("orchestrator", "note", "question received", detail=question,
              persona=persona)

    # Captured so callers can audit or re-check the exact text the commentary
    # was built from. Scraping it back out of the trace is lossy.
    captured: dict = {"findings": "", "persona": persona}

    def _commentary(findings: str, persona: str) -> dict:
        captured["findings"] = findings
        captured["persona"] = persona
        return _validated_commentary(persona, question, findings, trace)

    handlers = {
        "analyse_variance": lambda question: {
            "answer": variance_agent(question, trace).text},
        "analyse_reconciliation": lambda question: {
            "answer": reconciliation_agent(question, trace).text},
        "query_data": lambda question: {
            "answer": retrieval_agent(question, trace).text},
        "draft_commentary": _commentary,
    }

    prompt = f"Persona: {persona}\n\nQuestion: {question}"
    result: AgentResult = run_agent("orchestrator", ORCHESTRATOR_SYSTEM, prompt,
                                    TOOLS, handlers, trace)

    drafted = result.data.get("draft_commentary", [])

    if not drafted:
        # The orchestrator answered directly instead of routing through the
        # commentary agent. Its own prose was never checked against anything,
        # so rebuild the findings from what the specialists actually returned
        # and put it through the same validated path.
        gathered = []
        for tool in ("analyse_variance", "analyse_reconciliation", "query_data"):
            for call in result.data.get(tool, []):
                answer = (call or {}).get("answer", "").strip()
                if answer:
                    gathered.append(f"{tool.upper()}:\n{answer}")

        if gathered:
            trace.add("orchestrator", "note", "commentary step skipped",
                      detail="rebuilding findings from specialist output")
            captured["findings"] = "\n\n".join(gathered)
            drafted = [_commentary(captured["findings"], persona)]
        else:
            trace.add("orchestrator", "note", "no specialist output",
                      detail="answer is unvalidated orchestrator text")

    commentary = drafted[-1]["commentary"] if drafted else result.text

    validated = bool(drafted and drafted[-1].get("validated"))

    return {
        "question": question,
        "persona": persona,
        "answer": commentary,
        "validated": validated,
        "findings": captured["findings"],
        "orchestrator_text": result.text,
        "agents_used": trace.agents_used(),
        "sql_statements": trace.sql_statements(),
        "trace": trace,
    }


if __name__ == "__main__":
    q = " ".join(sys.argv[1:]) or "Where did we miss plan most badly, and why?"
    out = ask(q)

    print("=" * 70)
    print(out["answer"])
    print("=" * 70)
    print(f"\nAgents: {' -> '.join(out['agents_used'])}")
    print(f"Figures validated: {out['validated']}")
    print(f"SQL statements run: {len(out['sql_statements'])}")
    print("\nTrace")
    print(out["trace"].render())