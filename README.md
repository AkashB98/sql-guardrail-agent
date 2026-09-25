# sql-guardrail-agent

> **SIMULATED DATA** — the bundled `sample.db` is 100% fictional: invented
> customers, campaigns, and orders generated from a fixed random seed.
> No real people, companies, or figures anywhere in this repo.

A natural-language-to-SQL analytics agent that answers plain-English questions
about a database — **and refuses to do anything destructive while doing it**.

Ask *"Which marketing channel has the best ROI?"* → it generates SQL, proves
the SQL is read-only, estimates what the query will cost *before* running it,
then runs it and shows estimate vs. actual execution time side by side.
Ask *"Delete all orders from last year"* → it refuses, clearly and politely.

The point of this project isn't the text-to-SQL trick — it's the **safety
engineering around it**: a no-destructive-queries guardrail, query-cost
estimation, and a golden eval suite that scores both answer correctness and
refusal behavior. That's the part that matters when you ship an AI data
system to real users.

## Quickstart (one command)

```bash
python3 demo.py            # 3 example questions, end to end
python3 demo.py --refusals # watch the guardrail refuse 3 attacks
python3 demo.py --question "What is the revenue per campaign?"
python3 -m unittest discover -s tests   # 63 hermetic unit tests
python3 evals/run_evals.py --db sample.db  # 15-case golden eval suite
```

No API key, no network, no dependencies beyond the Python standard library
(`sqlite3`, `unittest`, `urllib`). Python 3.10+.

## How it works

```
question
  │  1. destructive-intent screen ("delete…", "drop…") → instant refusal
  ▼
SQL generation ── heuristic templates (offline, default)
  │                or OpenAI-compatible LLM (opt-in, env vars only)
  ▼
  │  2. guardrail.validate() — single read-only SELECT/WITH only
  ▼
  │  3. cost.estimate() — EXPLAIN QUERY PLAN → scans, warnings, cost tier
  ▼
read-only connection (mode=ro) → timed execution → answer + cost report
```

**Guardrail design** (`guardrail.py`) — allowlist, not blocklist:
- Only `SELECT` / `WITH … SELECT` statements may execute; exactly one
  statement per string (stacked queries refused).
- Comments are stripped *before* scanning, so obfuscation like
  `DR/**/OP TABLE t` collapses to `DROP TABLE t` and gets caught.
- Keyword scanning is string-literal aware: `WHERE name = 'Delete me'`
  is a harmless filter, not a command; `;` inside quotes isn't a
  statement separator.
- Forbidden anywhere executable: `DELETE DROP UPDATE INSERT ALTER CREATE
  TRUNCATE REPLACE VACUUM REINDEX PRAGMA ATTACH DETACH GRANT REVOKE`.
- Even if every check above were bypassed, queries run on a `mode=ro`
  connection — the database handle itself cannot write.

**Cost estimation** (`cost.py`) — honest heuristics, not a real optimizer:
- Parses `EXPLAIN QUERY PLAN`: `SCAN t` (full table scan) vs
  `SEARCH t USING …` / `SCAN t USING …` (indexed/covering-index access).
- Resolves table aliases (`SEARCH c USING …` → `campaigns`) and ignores
  CTE names so the report names real tables.
- Estimated rows touched is an *upper bound* from `COUNT(*)` on scanned
  tables (product of sizes when ≥2 unindexed scans → cartesian warning).
- Tiers: `HIGH` (full scan of a large table / possible cartesian),
  `MEDIUM` (small scan or temp B-tree sort), `LOW` (fully indexed).
- Warnings call out full-table scans on big tables and temp B-tree
  sorts/groups. Actual wall-clock time is measured and shown next to the
  estimate.

**SQL generation** (`agent.py`, `llm.py`): the default path is a
schema-aware template engine — deterministic, offline, and deliberately
narrow. It says *"I couldn't map that question"* instead of hallucinating
SQL it can't justify. Set `NL_SQL_LLM_ENDPOINT` + `NL_SQL_LLM_API_KEY`
(+ optional `NL_SQL_LLM_MODEL`) to route generation through any
OpenAI-compatible API; LLM-produced SQL passes through the same guardrail.

## Evals

`evals/run_evals.py` scores 15 golden cases: 12 analytics questions checked
by **result equivalence** against hand-written reference SQL, plus 3
destructive prompts that must be refused. Current: **15/15**
(report committed at `evals/eval_report.json`).

## Dev loop: what the tests actually caught

Real bugs, found by the test suite during this build — not invented for
the README:

1. **Leaked DB connection.** The first version of `ask()` opened a
   connection for SQL generation and never closed it. Refactored to a
   single connection passed through the whole pipeline.
2. **Phantom table in the cost report.** `EXPLAIN QUERY PLAN` reports
   aliases (`SEARCH c USING …`), so the report claimed indexed access on a
   table called `c`. Added alias resolution from the query's FROM/JOIN
   clauses.
3. **Covering-index scans fell through the cracks.** SQLite plans some
   queries as `SCAN orders USING COVERING INDEX …` — neither a bare scan
   nor a `SEARCH`, so the estimator silently reported them as zero-cost
   `LOW` with no access path at all. Now classified as indexed access.
4. **CTE names reported as tables.** A `WITH rev AS (…)` query showed
   "indexed access: rev". CTE names are now filtered from the report.
5. **Heuristic phrasing gaps.** The golden evals caught two phrasings the
   templates missed ("categories *have* an average order value above…",
   "how many orders did *each channel* drive") — patterns broadened, evals
   green.

## Sample data

`make_db.py` builds `sample.db` deterministically (seed 42): 200 customers,
12 campaigns across 4 channels, 1,500 orders from Jan–Sep 2026. Every name,
city, and dollar figure is invented. Rebuild anytime with
`python3 make_db.py sample.db`.

## Why this name

An older, unrelated experiment from May 2026 already lives at
`ai-sql-analytics-agent`. This is a fresh build with a distinct name so the
two can't be confused.

## License

MIT — see `LICENSE`.
