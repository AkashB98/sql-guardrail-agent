"""Golden eval runner: score the agent on the fixed NL question set.

For `sql` cases the agent's answer is scored by RESULT EQUIVALENCE against a
hand-written reference query (same rows, order-insensitive, floats rounded to
2dp) — plus a secondary exact-SQL-match metric. For `refuse` cases the agent
must refuse.

Writes evals/eval_report.json. Exit code 0 only if every case passes.
Run:  python evals/run_evals.py [--db PATH]
"""
import json
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent  # noqa: E402
import make_db  # noqa: E402

GOLDEN_PATH = os.path.join(os.path.dirname(__file__), "golden.json")
REPORT_PATH = os.path.join(os.path.dirname(__file__), "eval_report.json")


def _normalize(sql: str) -> str:
    return " ".join(sql.lower().split())


def _canon(rows):
    out = []
    for r in rows:
        out.append(tuple(round(v, 2) if isinstance(v, float) else v for v in r))
    return sorted(out)


def run(db_path: str) -> dict:
    with open(GOLDEN_PATH) as f:
        cases = json.load(f)
    conn = agent.connect(db_path)
    results = []
    for case in cases:
        ans = agent.ask(db_path, case["question"])
        if case["type"] == "refuse":
            passed = ans.refused and not ans.ok
            detail = {"refused": ans.refused}
        else:
            try:
                ref_rows = _canon(conn.execute(case["reference_sql"]).fetchall())
            except sqlite3.Error as exc:
                passed, detail = False, {"error": f"bad reference SQL: {exc}"}
            else:
                got_rows = _canon(ans.rows) if ans.ok else None
                result_equiv = ans.ok and got_rows == ref_rows
                sql_match = _normalize(ans.sql) == _normalize(case["reference_sql"])
                passed = bool(result_equiv)
                detail = {"result_equivalent": bool(result_equiv),
                          "sql_match": bool(sql_match),
                          "backend": ans.backend,
                          "cost_tier": ans.cost.cost_tier if ans.cost else None}
            detail["sql"] = ans.sql
        results.append({"id": case["id"], "question": case["question"],
                        "type": case["type"], "passed": passed,
                        "detail": detail})
    conn.close()

    sql_cases = [r for r in results if r["type"] == "sql"]
    refuse_cases = [r for r in results if r["type"] == "refuse"]
    report = {
        "summary": {
            "total": len(results),
            "passed": sum(r["passed"] for r in results),
            "sql_cases": len(sql_cases),
            "sql_passed": sum(r["passed"] for r in sql_cases),
            "refuse_cases": len(refuse_cases),
            "refuse_passed": sum(r["passed"] for r in refuse_cases),
            "score": round(sum(r["passed"] for r in results) / len(results), 3),
        },
        "results": results,
    }
    return report


def main() -> int:
    db_path = sys.argv[sys.argv.index("--db") + 1] \
        if "--db" in sys.argv else None
    tmpdir = None
    if db_path is None:
        # Hermetic: build a fresh seeded database per run.
        tmpdir = tempfile.mkdtemp(prefix="sqlguard_eval_")
        db_path = os.path.join(tmpdir, "eval.db")
        make_db.build(db_path)
    report = run(db_path)
    with open(REPORT_PATH, "w") as f:
        json.dump(report, f, indent=2)
    s = report["summary"]
    print(f"score: {s['passed']}/{s['total']} "
          f"(sql {s['sql_passed']}/{s['sql_cases']}, "
          f"refusals {s['refuse_passed']}/{s['refuse_cases']})")
    for r in report["results"]:
        mark = "PASS" if r["passed"] else "FAIL"
        print(f"  [{mark}] {r['id']}: {r['question'][:60]}")
        if not r["passed"]:
            print(f"         detail: {json.dumps(r['detail'])[:300]}")
    print(f"report: {REPORT_PATH}")
    return 0 if s["passed"] == s["total"] else 1


if __name__ == "__main__":
    sys.exit(main())
