"""NL -> SQL analytics agent.

Pipeline for one question:
  1. Destructive-intent screen on the English question ("delete all orders")
     -> immediate refusal, no SQL is ever generated.
  2. SQL generation: built-in heuristic templates (offline, no key needed),
     or an optional OpenAI-compatible LLM backend (llm.py) when the
     NL_SQL_LLM_* env vars are set.
  3. Guardrail: guardrail.validate() — only single read-only SELECT/WITH
     statements may proceed. LLM output is gated here too.
  4. Cost estimation: cost.estimate() runs EXPLAIN QUERY PLAN and reports
     full-table scans, cartesian-product risk, an estimated-rows-touched
     upper bound, and a LOW/MEDIUM/HIGH tier.
  5. Execution on a READ-ONLY database handle (mode=ro), timed, rows capped.

Nothing here ever writes to the database: the guardrail is the policy gate
and the read-only connection is the backstop.
"""
from __future__ import annotations

import re
import sqlite3
import time
from dataclasses import dataclass, field

import cost as cost_mod
import guardrail
import llm

DISPLAY_LIMIT = 100

_MONTHS = {
    "january": "01", "february": "02", "march": "03", "april": "04",
    "may": "05", "june": "06", "july": "07", "august": "08",
    "september": "09", "october": "10", "november": "11", "december": "12",
}

# Intent screen: English that asks for a destructive operation never reaches
# SQL generation at all.
_HARD_DESTRUCTIVE = re.compile(r"\b(delete|drop|truncate)\b", re.IGNORECASE)
_SOFT_DESTRUCTIVE = re.compile(
    r"\b(update|insert|alter|create)\b\s+"
    r"(the|all|into|orders|customers|campaigns|table|data|records|rows)\b",
    re.IGNORECASE,
)


@dataclass
class Answer:
    question: str
    ok: bool
    refused: bool = False
    message: str = ""
    sql: str = ""
    backend: str = ""          # "heuristic" | "llm"
    columns: list = field(default_factory=list)
    rows: list = field(default_factory=list)
    row_count: int = 0         # total rows (display capped at DISPLAY_LIMIT)
    cost: object = None        # cost_mod.CostReport
    elapsed_ms: float = 0.0


def _is_destructive_intent(question: str) -> bool:
    return bool(_HARD_DESTRUCTIVE.search(question)
                or _SOFT_DESTRUCTIVE.search(question))


def _top_n(q: str, default: int = 5) -> int:
    m = re.search(r"\btop\s+(\d+)\b", q)
    return int(m.group(1)) if m else default


def _heuristic_sql(question: str, conn: sqlite3.Connection) -> str | None:
    """Template-based NL->SQL. Returns None when nothing matches."""
    q = question.lower().strip()

    # --- destructive intent is screened before this is ever called ---

    if re.search(r"\btop\s+\d+\s+campaigns?\s+by\s+revenue\b", q):
        n = _top_n(q)
        return (
            "SELECT c.campaign_name, ROUND(SUM(o.amount), 2) AS revenue "
            "FROM orders o JOIN campaigns c ON o.campaign_id = c.campaign_id "
            "GROUP BY c.campaign_name ORDER BY revenue DESC "
            f"LIMIT {n}"
        )
    if re.search(r"\brevenue\s+(per|by)\s+campaign\b", q):
        return (
            "SELECT c.campaign_name, ROUND(SUM(o.amount), 2) AS revenue "
            "FROM orders o JOIN campaigns c ON o.campaign_id = c.campaign_id "
            "GROUP BY c.campaign_name ORDER BY revenue DESC"
        )
    if re.search(r"\broi\b", q):
        return (
            "WITH rev AS (SELECT campaign_id, SUM(amount) AS revenue "
            "FROM orders GROUP BY campaign_id) "
            "SELECT c.channel, "
            "ROUND((SUM(COALESCE(rev.revenue, 0)) - SUM(c.spend)) "
            "/ NULLIF(SUM(c.spend), 0), 3) AS roi "
            "FROM campaigns c LEFT JOIN rev ON rev.campaign_id = c.campaign_id "
            "GROUP BY c.channel ORDER BY roi DESC"
        )
    if re.search(r"\btop\s+\d+\s+customers?\s+by\s+(total\s+)?spend\b", q):
        n = _top_n(q)
        return (
            "SELECT cu.full_name, ROUND(SUM(o.amount), 2) AS total_spend "
            "FROM orders o JOIN customers cu ON o.customer_id = cu.customer_id "
            "GROUP BY cu.full_name ORDER BY total_spend DESC "
            f"LIMIT {n}"
        )
    if re.search(r"average\s+order\s+value\s+by\s+category", q):
        return (
            "SELECT category, ROUND(AVG(amount), 2) AS avg_order_value, "
            "COUNT(*) AS orders "
            "FROM orders GROUP BY category ORDER BY avg_order_value DESC"
        )
    m = re.search(
        r"categor\w*\s+(?:with|having|have|has)\s+(?:an?\s+)?average\s+order\s+value\s+"
        r"(?:above|over|greater\s+than)\s+(\d+(?:\.\d+)?)", q)
    if m:
        threshold = m.group(1)
        return (
            "SELECT category, ROUND(AVG(amount), 2) AS avg_order_value "
            f"FROM orders GROUP BY category HAVING AVG(amount) > {threshold} "
            "ORDER BY avg_order_value DESC"
        )
    if re.search(r"\btotal\s+revenue\b|\btotal\s+sales\b", q):
        return "SELECT ROUND(SUM(amount), 2) AS total_revenue FROM orders"
    if re.search(r"\btotal\b.*\bmarketing\s+spend\b|\btotal\b.*\bspend\b", q):
        return "SELECT ROUND(SUM(spend), 2) AS total_spend FROM campaigns"
    if re.search(r"how\s+many\s+customers", q):
        return "SELECT COUNT(*) AS customer_count FROM customers"
    m = re.search(r"\borders?\b.*\bin\s+([a-z]+)\s+(\d{4})\b", q)
    if m and m.group(1) in _MONTHS:
        ym = f"{m.group(2)}-{_MONTHS[m.group(1)]}"
        return (
            "SELECT COUNT(*) AS order_count FROM orders "
            f"WHERE strftime('%Y-%m', order_date) = '{ym}'"
        )
    if (re.search(r"\borders?\s+(per|by)\s+channel\b", q)
            or ("channel" in q and "order" in q
                and re.search(r"\bhow\s+many\b|\beach\b", q))):
        return (
            "SELECT c.channel, COUNT(*) AS orders "
            "FROM orders o JOIN campaigns c ON o.campaign_id = c.campaign_id "
            "GROUP BY c.channel ORDER BY orders DESC"
        )
    m = re.search(r"customers?\s+from\s+([a-z ]+?)[?.!]*$", q)
    if m:
        city = m.group(1).strip().title()
        known = {r[0] for r in
                 conn.execute("SELECT DISTINCT city FROM customers").fetchall()}
        if city in known:
            return (
                "SELECT full_name, city, signup_date FROM customers "
                f"WHERE city = '{city}' ORDER BY full_name"
            )
        # Unknown city: still a valid read-only query, returns zero rows.
        return (
            "SELECT full_name, city, signup_date FROM customers "
            f"WHERE city = '{city}' ORDER BY full_name"
        )
    return None


def generate_sql(question: str, conn: sqlite3.Connection | None,
                 use_llm: bool = False) -> tuple[str, str]:
    """Return (sql, backend). Raises ValueError if the question is unmapped."""
    if _is_destructive_intent(question):
        raise ValueError("destructive intent")
    if use_llm or llm.is_configured():
        return llm.generate(question), "llm"
    if conn is None:
        raise ValueError("no database connection for heuristic generation")
    sql = _heuristic_sql(question, conn)
    if sql is None:
        raise ValueError(
            "I couldn't map that question to the demo schema. Try asking "
            "about revenue, campaigns, channels, customers, categories, or "
            "order counts — e.g. 'What is the revenue per campaign?'")
    return sql, "heuristic"


def connect(db_path: str) -> sqlite3.Connection:
    """Open the database READ-ONLY. Writes are impossible on this handle."""
    return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)


def ask(db_path: str, question: str, use_llm: bool = False) -> Answer:
    """Run the full pipeline for one natural-language question."""
    ans = Answer(question=question, ok=False)
    conn = connect(db_path)
    try:
        try:
            sql, backend = generate_sql(question, conn, use_llm)
        except ValueError as exc:
            msg = str(exc)
            if msg == "destructive intent":
                ans.refused, ans.message = True, guardrail.REFUSAL_MESSAGE
            else:
                ans.message = msg
            return ans

        ans.sql, ans.backend = sql, backend
        decision = guardrail.validate(sql)
        if not decision.ok:
            ans.refused = True
            ans.message = f"{guardrail.REFUSAL_MESSAGE} ({decision.reason})"
            return ans

        ans.cost = cost_mod.estimate(conn, sql)
        start = time.perf_counter()
        cur = conn.execute(sql)
        ans.columns = [d[0] for d in cur.description] if cur.description else []
        all_rows = cur.fetchall()
        ans.elapsed_ms = (time.perf_counter() - start) * 1000
        ans.row_count = len(all_rows)
        ans.rows = all_rows[:DISPLAY_LIMIT]
        ans.ok = True
    except sqlite3.Error as exc:
        ans.message = f"Query failed to execute: {exc}"
    finally:
        conn.close()
    return ans


def format_answer(ans: Answer) -> str:
    """Human-readable rendering for the CLI demo."""
    lines = [f"Q: {ans.question}"]
    if ans.refused:
        return "\n".join(lines + ["", "REFUSED", ans.message])
    if not ans.ok:
        return "\n".join(lines + ["", "NO ANSWER", ans.message])
    lines.append(f"SQL [{ans.backend}]: {ans.sql}")
    lines.append("")
    if ans.columns:
        widths = [max(len(str(c)), *(len(str(r[i])) for r in ans.rows))
                  for i, c in enumerate(ans.columns)]
        lines.append(" | ".join(str(c).ljust(w)
                                for c, w in zip(ans.columns, widths)))
        lines.append("-+-".join("-" * w for w in widths))
        for r in ans.rows:
            lines.append(" | ".join(str(v).ljust(w)
                                    for v, w in zip(r, widths)))
        if ans.row_count > len(ans.rows):
            lines.append(f"... ({ans.row_count - len(ans.rows)} more rows)")
    lines.append("")
    lines.append(f"actual execution time: {ans.elapsed_ms:.1f} ms")
    if ans.cost is not None:
        lines.append(ans.cost.summary())
    return "\n".join(lines)
