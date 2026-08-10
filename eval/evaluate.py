"""
Batch evaluation of the agent layer.

Reading one response carefully tells you whether that response was wrong. It
does not tell you how often the system is wrong, and it cannot show that a fix
worked. This runs a fixed question set repeatedly and reports two things that
can go in a README:

  figure accuracy   share of answers where every number traces to the engine
  stability         whether repeated runs of one question agree

Run:  python -m eval.evaluate
      python -m eval.evaluate --repeats 3 --out eval/results.json
"""

import argparse
import json
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from agents.base import Trace
from agents.orchestrator import ask
from agents.validator import extract_numbers, validate_commentary

# Each question names its ranking basis, its scope and its period. Ambiguity
# here shows up as instability in the results and is indistinguishable from a
# genuine defect, so it is removed at source: "most exceeded plan" could mean
# dollars or percentage, and the agent will pick differently on each run.
QUESTIONS = [
    ("largest_usd_shortfall",
     "Ranking by absolute USD, which single cost centre and month had the "
     "largest unfavourable variance across the whole dataset? Give the total "
     "variance and each of the four drivers in dollars."),
    ("largest_beat",
     "Ranking by absolute USD, which single cost centre and month had the "
     "largest favourable variance across the whole dataset? Give the total "
     "variance and each of the four drivers in dollars."),
    ("recon_summary",
     "Across the entire dataset, give the count and total USD value of "
     "reconciliation breaks for every break type."),
    ("fx_impact",
     "Across the entire dataset, what is the total FX component of the "
     "budget-to-actual bridge in USD, and what are the total budget and total "
     "actual?"),
    ("missing_revenue",
     "Across the entire dataset, how many transactions closed in CRM never "
     "reached the general ledger, and what is their total USD value?"),
    ("product_ranking",
     "Across the entire dataset, rank all product lines by total revenue in "
     "erp.gl_entries, highest first, with the USD total for each."),
]


HEADLINE_N = 3


def key_figures(text: str) -> tuple:
    """
    The largest few magnitudes quoted, rounded so $171k and 170,896.94 match.

    Comparing every figure in an answer measures verbosity, not correctness: one
    run mentioning an extra supporting number would read as unstable even when
    the headline finding is identical. The largest few figures are the claim.
    """
    return tuple(sorted(all_figures(text), reverse=True)[:HEADLINE_N])


def all_figures(text: str) -> set:
    """Every material magnitude quoted, rounded to the nearest hundred."""
    return {round(abs(v), -2) for _, v, is_pct in extract_numbers(text)
            if not is_pct and abs(v) >= 1000}


def overlap(a: set, b: set) -> float:
    """Jaccard overlap, so partial agreement is visible rather than binary."""
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def run_once(qid: str, question: str) -> dict:
    """
    One run. Validation is taken from the orchestrator, which checked the
    commentary against the findings object itself. Re-deriving the findings
    from the trace text here would be a worse copy of the same check, and
    disagreements between the two are measurement noise rather than defects.
    """
    trace = Trace()
    t0 = time.time()
    try:
        result = ask(question, trace=trace)
        answer = result["answer"]
        error = None
    except Exception as exc:
        answer, result, error = "", {}, f"{type(exc).__name__}: {exc}"

    findings = result.get("findings", "")
    check = validate_commentary(answer, findings) if answer and findings else None

    return {
        "question_id": qid,
        "question": question,
        "answer": answer,
        "error": error,
        "elapsed_s": round(time.time() - t0, 1),
        "agents": result.get("agents_used", []),
        "sql_count": len(result.get("sql_statements", [])),
        "validated": result.get("validated", False),
        "unsupported": check.unsupported if check else [],
        "percentages": check.percentages if check else [],
        "figures": list(key_figures(answer)),
        "all_figures": sorted(all_figures(answer)),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repeats", type=int, default=2,
                        help="runs per question")
    parser.add_argument("--out", default="eval/results.json")
    args = parser.parse_args()

    runs = []
    for qid, question in QUESTIONS:
        for i in range(args.repeats):
            print(f"  {qid} ({i + 1}/{args.repeats}) ...", end="", flush=True)
            record = run_once(qid, question)
            runs.append(record)
            flag = "ok" if record["validated"] and not record["error"] else "FLAG"
            print(f" {record['elapsed_s']}s  {flag}")

    by_q = defaultdict(list)
    for r in runs:
        by_q[r["question_id"]].append(r)

    total = len(runs)
    errored = sum(1 for r in runs if r["error"])

    # Accuracy is measured over runs that actually executed. An exhausted API
    # balance or a network fault is an infrastructure failure, and counting it
    # as a wrong answer is the same category of measurement error the validator
    # exists to avoid.
    completed = [r for r in runs if not r["error"]]
    validated = sum(1 for r in completed if r["validated"])
    ok_by_q = {q: [r for r in rs if not r["error"]] for q, rs in by_q.items()}
    ok_by_q = {q: rs for q, rs in ok_by_q.items() if len(rs) > 1}

    stable = sum(1 for rs in ok_by_q.values()
                 if len({tuple(r["figures"]) for r in rs}) == 1)

    overlaps = []
    for rs in ok_by_q.values():
        sets = [set(r["all_figures"]) for r in rs]
        pairs = [overlap(sets[i], sets[j])
                 for i in range(len(sets)) for j in range(i + 1, len(sets))]
        if pairs:
            overlaps.append(sum(pairs) / len(pairs))

    summary = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "questions": len(by_q),
        "repeats": args.repeats,
        "runs": total,
        "runs_completed": len(completed),
        "figure_accuracy_pct": round(validated / len(completed) * 100, 1)
        if completed else 0,
        "questions_compared": len(ok_by_q),
        "headline_stable_questions": stable,
        "headline_stability_pct": round(stable / len(ok_by_q) * 100, 1)
        if ok_by_q else 0,
        "mean_figure_overlap_pct": round(sum(overlaps) / len(overlaps) * 100, 1)
        if overlaps else 0,
        "errors": errored,
        "mean_seconds": round(
            sum(r["elapsed_s"] for r in completed) / len(completed), 1)
        if completed else 0,
        "mean_sql_calls": round(
            sum(r["sql_count"] for r in completed) / len(completed), 1)
        if completed else 0,
    }

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"summary": summary, "runs": runs}, indent=2),
                   encoding="utf-8")

    print("\n" + "=" * 60)
    for k, v in summary.items():
        print(f"  {k:24} {v}")
    print("=" * 60)

    infra = [r for r in runs if r["error"]]
    if infra:
        print(f"\n{len(infra)} run(s) did not execute (excluded from accuracy):")
        for r in infra:
            print(f"  {r['question_id']}: {r['error'][:120]}")

    failed = [r for r in completed if not r["validated"]]
    if failed:
        print(f"\n{len(failed)} run(s) failed validation:")
        for r in failed:
            print(f"  {r['question_id']}: unsupported {r['unsupported']} "
                  f"percentages {r['percentages']}")

    unstable = [q for q, rs in by_q.items()
                if len({tuple(r["figures"]) for r in rs
                        if not r["error"]}) > 1]
    if unstable:
        print(f"\nUnstable across repeats: {', '.join(unstable)}")

    print(f"\nFull results: {out}")


if __name__ == "__main__":
    main()