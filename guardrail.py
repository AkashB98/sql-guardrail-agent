"""No-destructive-queries guardrail.

Defense in depth for the NL->SQL agent: every SQL string — whether it came
from the built-in heuristic generator or an optional LLM backend — passes
through `validate()` before it may touch the database.

Policy:
  * Only read-only statements run: SELECT, or WITH ... SELECT (CTEs).
  * Exactly one statement per string. Stacked queries (`; ...`) are refused.
  * Destructive / write / DDL keywords are refused anywhere they appear
    outside of string literals: DROP, DELETE, UPDATE, INSERT, ALTER, CREATE,
    TRUNCATE, REPLACE, VACUUM, REINDEX, PRAGMA, ATTACH, DETACH, GRANT, REVOKE.
  * SQL comments are stripped *before* scanning, so obfuscation tricks like
    ``DR/**/OP TABLE t`` collapse back to ``DROP TABLE t`` and get caught.
  * Keywords inside quoted string literals are ignored — e.g.
    ``WHERE full_name = 'Delete me'`` is a harmless filter, not a command.

This is a syntactic gate, not a sandbox: it cannot make a SELECT safe against
every exotic SQLite extension, so the demo database is additionally opened in
a way that never writes (queries run on a read path; see agent.py).
"""
from __future__ import annotations

from dataclasses import dataclass

# Keywords that must never appear as executable tokens.
FORBIDDEN = {
    "DELETE", "DROP", "UPDATE", "INSERT", "ALTER", "CREATE", "TRUNCATE",
    "REPLACE", "VACUUM", "REINDEX", "PRAGMA", "ATTACH", "DETACH",
    "GRANT", "REVOKE",
}

# Statements allowed to execute.
ALLOWED_FIRST = {"SELECT", "WITH"}


@dataclass
class GuardrailDecision:
    ok: bool
    reason: str = ""


def _strip_comments(sql: str) -> str:
    """Remove -- and /* */ comments, respecting quoted string literals."""
    out: list[str] = []
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < n else ""
        if ch in ("'", '"'):
            # copy the whole string literal verbatim ('' is an escaped quote)
            out.append(ch)
            i += 1
            while i < n:
                out.append(sql[i])
                if sql[i] == ch:
                    if i + 1 < n and sql[i + 1] == ch:
                        out.append(sql[i + 1])
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
        elif ch == "-" and nxt == "-":
            while i < n and sql[i] != "\n":
                i += 1
        elif ch == "/" and nxt == "*":
            i += 2
            while i + 1 < n and not (sql[i] == "*" and sql[i + 1] == "/"):
                i += 1
            i += 2  # skip closing */
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def _split_statements(sql: str) -> list[str]:
    """Split on semicolons that are outside string literals."""
    parts, cur = [], []
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        if ch in ("'", '"'):
            cur.append(ch)
            i += 1
            while i < n:
                cur.append(sql[i])
                if sql[i] == ch:
                    if i + 1 < n and sql[i + 1] == ch:
                        cur.append(sql[i + 1])
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
        elif ch == ";":
            parts.append("".join(cur))
            cur = []
            i += 1
        else:
            cur.append(ch)
            i += 1
    parts.append("".join(cur))
    return [p for p in parts if p.strip()]


def _tokens_outside_strings(sql: str) -> list[str]:
    """Upper-cased word tokens found outside quoted string literals."""
    tokens: list[str] = []
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        if ch in ("'", '"'):
            i += 1
            while i < n:
                if sql[i] == ch:
                    if i + 1 < n and sql[i + 1] == ch:
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
        elif ch.isalpha() or ch == "_":
            j = i
            while j < n and (sql[j].isalnum() or sql[j] == "_"):
                j += 1
            tokens.append(sql[i:j].upper())
            i = j
        else:
            i += 1
    return tokens


def validate(sql: str) -> GuardrailDecision:
    """Decide whether a SQL string is safe to execute. Never raises."""
    if not sql or not sql.strip():
        return GuardrailDecision(False, "Refused: empty query.")

    cleaned = _strip_comments(sql)
    statements = _split_statements(cleaned)
    if not statements:
        return GuardrailDecision(False, "Refused: empty query.")
    if len(statements) > 1:
        return GuardrailDecision(
            False,
            "Refused: multiple statements detected — stacked queries are not allowed.",
        )

    stmt = statements[0]
    tokens = _tokens_outside_strings(stmt)
    if not tokens:
        return GuardrailDecision(False, "Refused: empty query.")
    if tokens[0] not in ALLOWED_FIRST:
        return GuardrailDecision(
            False,
            f"Refused: only read-only SELECT queries may run "
            f"(found leading keyword {tokens[0]}).",
        )
    bad = sorted({t for t in tokens if t in FORBIDDEN})
    if bad:
        return GuardrailDecision(
            False,
            "Refused: disallowed keyword(s) detected: " + ", ".join(bad) + ".",
        )
    return GuardrailDecision(True, "OK: read-only SELECT query.")


REFUSAL_MESSAGE = (
    "I can't run that — this agent is read-only by design. "
    "Destructive operations (DELETE, DROP, UPDATE, INSERT, schema changes) "
    "and multi-statement queries are blocked by the guardrail. "
    "Ask me an analytics question instead, e.g. "
    "'What is the total revenue by campaign?'"
)
