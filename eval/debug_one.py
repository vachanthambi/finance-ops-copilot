"""
Debug one question end to end.

Prints the findings the commentary was built from, the answer produced, and
exactly which figures failed validation. One question, one API round trip, so
it costs cents rather than dollars -- use this instead of re-running the whole
evaluation suite to chase a single failure.

Run:
    python -m eval.debug_one
    python -m eval.debug_one "your question here"
"""

import sys

from agents.base import Trace
from agents.orchestrator import ask
from agents.validator import extract_numbers, validate_commentary

DEFAULT_QUESTION = (
    "Across the entire dataset, how many transactions closed in CRM never "
    "reached the general ledger, and what is their total USD value?"
)


def rule(label: str) -> None:
    print("\n" + "=" * 72)
    print(label)
    print("=" * 72)


def main() -> None:
    question = " ".join(sys.argv[1:]).strip() or DEFAULT_QUESTION

    trace = Trace()
    result = ask(question, trace=trace)

    rule("QUESTION")
    print(question)

    rule("FINDINGS PASSED TO COMMENTARY")
    print(result["findings"] or "(empty - the orchestrator passed nothing)")

    rule("ANSWER")
    print(result["answer"])

    rule("VALIDATION")
    check = validate_commentary(result["answer"], result["findings"])
    print(f"orchestrator verdict : {result['validated']}")
    print(f"independent recheck  : {check.ok}")
    print(f"figures checked      : {check.checked}")
    print(f"unsupported          : {check.unsupported or 'none'}")
    print(f"percentages          : {check.percentages or 'none'}")

    rule("FIGURES SIDE BY SIDE")
    findings_figs = [raw for raw, _, _ in extract_numbers(result["findings"])]
    answer_figs = [raw for raw, _, _ in extract_numbers(result["answer"])]
    print(f"in findings ({len(findings_figs)}):")
    print("  " + ", ".join(findings_figs) or "  none")
    print(f"\nin answer ({len(answer_figs)}):")
    print("  " + ", ".join(answer_figs) or "  none")

    rule("AGENT PATH")
    print(" -> ".join(result["agents_used"]))
    print(f"SQL statements: {len(result['sql_statements'])}")

    for step in trace.steps:
        if step.agent == "validator":
            print(f"[validator] {step.label}: {step.detail}")


if __name__ == "__main__":
    main()
