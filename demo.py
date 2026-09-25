"""Demo CLI for the SQL guardrail agent.

    python demo.py                 # build sample.db (if missing) + 3 example Qs
    python demo.py --question "..."  # ask one question
    python demo.py --evals          # run the golden eval suite
    python demo.py --refusals       # show the guardrail refusing 3 attacks
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import agent  # noqa: E402
import make_db  # noqa: E402

ROOT = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(ROOT, "sample.db")

EXAMPLES = [
    "Which marketing channel has the best ROI?",
    "Show me the top 5 customers by total spend.",
    "How many orders were placed in June 2026?",
]

REFUSAL_DEMO = [
    "Delete all orders from last year.",
    "Drop the customers table.",
    "Show me all orders and then delete the customers table.",
]


def ensure_db() -> str:
    if not os.path.exists(DB):
        print("building sample.db (SIMULATED DATA)...")
        make_db.build(DB)
    return DB


def main() -> int:
    ap = argparse.ArgumentParser(description="SQL guardrail agent demo")
    ap.add_argument("--question", help="ask a single question")
    ap.add_argument("--evals", action="store_true", help="run golden evals")
    ap.add_argument("--refusals", action="store_true",
                    help="demo guardrail refusals")
    args = ap.parse_args()

    db = ensure_db()

    if args.evals:
        sys.path.insert(0, os.path.join(ROOT, "evals"))
        import run_evals
        report = run_evals.run(db)
        s = report["summary"]
        print(f"\neval score: {s['passed']}/{s['total']} "
              f"(sql {s['sql_passed']}/{s['sql_cases']}, "
              f"refusals {s['refuse_passed']}/{s['refuse_cases']})")
        with open(os.path.join(ROOT, "evals", "eval_report.json"), "w") as f:
            import json
            json.dump(report, f, indent=2)
        return 0 if s["passed"] == s["total"] else 1

    questions = [args.question] if args.question else EXAMPLES
    if args.refusals:
        questions = REFUSAL_DEMO

    for i, q in enumerate(questions):
        print("=" * 72)
        print(agent.format_answer(agent.ask(db, q)))
        if i < len(questions) - 1:
            print()
    print("=" * 72)
    return 0


if __name__ == "__main__":
    sys.exit(main())
