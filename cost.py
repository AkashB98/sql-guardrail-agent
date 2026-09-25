"""Query-cost estimation via EXPLAIN QUERY PLAN.

For every query the agent runs, we:
  1. Ask SQLite for its query plan (EXPLAIN QUERY PLAN).
  2. Classify each plan node: full table SCAN vs indexed SEARCH ... USING.
  3. Estimate rows touched as a heuristic upper bound from table row counts.
  4. Emit warnings (full-table scan on a big table, possible cartesian
     product, temp B-tree sorts) and a LOW/MEDIUM/HIGH cost tier.
  5. The agent also records the *actual* wall-clock execution time, so the
     demo shows estimate vs reality side by side.

Honest caveat: these are heuristic estimates, not a real cost-based
optimizer. SQLite's EXPLAIN QUERY PLAN gives access paths, not cardinalities,
so "estimated rows touched" is an upper bound derived from COUNT(*) on the
tables that get fully scanned.
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field

# A full scan of a table bigger than this earns a warning.
LARGE_TABLE_ROWS = 500


@dataclass
class PlanNode:
    id: int
    parent: int
    detail: str


@dataclass
class CostReport:
    plan: list[PlanNode] = field(default_factory=list)
    full_scans: list[str] = field(default_factory=list)   # tables fully scanned
    indexed: list[str] = field(default_factory=list)     # tables accessed via index
    uses_temp_btree: bool = False
    estimated_rows_touched: int = 0
    warnings: list[str] = field(default_factory=list)
    cost_tier: str = "LOW"

    def summary(self) -> str:
        lines = [
            f"cost tier: {self.cost_tier}",
            f"estimated rows touched (upper bound): {self.estimated_rows_touched:,}",
        ]
        if self.full_scans:
            lines.append("full table scans: " + ", ".join(self.full_scans))
        if self.indexed:
            lines.append("indexed access: " + ", ".join(self.indexed))
        for w in self.warnings:
            lines.append("warning: " + w)
        return "\n".join(lines)


_SCAN_RE = re.compile(r"^SCAN\s+(\S+)", re.IGNORECASE)
_SCAN_USING_RE = re.compile(r"^SCAN\s+(\S+)\s+USING\b", re.IGNORECASE)
_SEARCH_RE = re.compile(r"^SEARCH\s+(\S+)\s+USING", re.IGNORECASE)
_CTE_RE = re.compile(r"\bWITH\b\s+(\w+)\s+AS\b", re.IGNORECASE)
_ALIAS_RE = re.compile(
    r"\b(?:FROM|JOIN)\s+\"?(\w+)\"?(?:\s+(?:AS\s+)?\"?(\w+)\"?)?",
    re.IGNORECASE,
)
_SQL_KEYWORDS = {
    "SELECT", "WHERE", "GROUP", "ORDER", "LIMIT", "HAVING", "UNION",
    "LEFT", "RIGHT", "INNER", "OUTER", "CROSS", "ON", "USING",
}


def _alias_map(sql: str) -> dict[str, str]:
    """Map table aliases (as EXPLAIN QUERY PLAN reports them) to real tables.

    Bug this fixes: the plan says ``SEARCH c USING ...`` when the query
    aliases ``campaigns`` as ``c`` — reporting "c" as the table name is wrong.
    """
    mapping: dict[str, str] = {}
    for table, alias in _ALIAS_RE.findall(sql):
        mapping[table] = table  # the real name always resolves to itself
        if alias and alias.upper() not in _SQL_KEYWORDS:
            mapping[alias] = table
    return mapping


def _table_row_counts(conn: sqlite3.Connection, tables: list[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    known = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    for t in tables:
        # table names come from SQLite's own plan output, but stay safe anyway
        if t in known:
            counts[t] = conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
        else:
            counts[t] = 0
    return counts


def estimate(conn: sqlite3.Connection, sql: str) -> CostReport:
    """Build a CostReport for a (guardrail-approved) SELECT query."""
    report = CostReport()
    try:
        rows = conn.execute("EXPLAIN QUERY PLAN " + sql).fetchall()
    except sqlite3.Error as exc:  # e.g. SQL the planner itself rejects
        report.warnings.append(f"planner error: {exc}")
        report.cost_tier = "UNKNOWN"
        return report

    for row in rows:
        _id, parent, _notused, detail = row[0], row[1], row[2], row[3]
        report.plan.append(PlanNode(_id, parent, detail))
        aliases = _alias_map(sql)
        ctes = set(_CTE_RE.findall(sql))

        def real(name: str) -> str | None:
            resolved = aliases.get(name, name)
            return None if resolved in ctes or name in ctes else resolved

        m = _SCAN_USING_RE.match(detail) or _SEARCH_RE.match(detail)
        if m:
            table = real(m.group(1))
            if table:
                report.indexed.append(table)
            continue
        m = _SCAN_RE.match(detail)
        if m:
            table = real(m.group(1))
            if table:
                report.full_scans.append(table)
            continue
        if "TEMP B-TREE" in detail.upper():
            report.uses_temp_btree = True

    # de-dupe, keep order
    report.full_scans = list(dict.fromkeys(report.full_scans))
    report.indexed = list(dict.fromkeys(report.indexed))

    counts = _table_row_counts(conn, report.full_scans)

    # Heuristic upper bound on rows touched: if two or more tables are fully
    # scanned, the join can in the worst case touch the product (cartesian);
    # otherwise the bound is the scanned rows.
    if len(report.full_scans) >= 2:
        bound = 1
        for t in report.full_scans:
            bound *= max(counts.get(t, 0), 1)
        report.estimated_rows_touched = bound
        report.warnings.append(
            "multiple full-table scans with no indexed join — "
            "worst case is a cartesian product; "
            "estimated rows touched is the product of table sizes."
        )
    elif report.full_scans:
        t = report.full_scans[0]
        report.estimated_rows_touched = counts.get(t, 0)
        if counts.get(t, 0) >= LARGE_TABLE_ROWS:
            report.warnings.append(
                f"full-table scan on '{t}' ({counts[t]:,} rows) — "
                "consider whether an index could serve this query."
            )
    else:
        report.estimated_rows_touched = 0  # fully indexed path

    if report.uses_temp_btree and report.estimated_rows_touched >= LARGE_TABLE_ROWS:
        report.warnings.append(
            "query sorts/groups via a temporary B-tree over a large input.")

    # Tier assignment (documented heuristic).
    if len(report.full_scans) >= 2:
        report.cost_tier = "HIGH"
    elif report.full_scans and counts.get(report.full_scans[0], 0) >= LARGE_TABLE_ROWS:
        report.cost_tier = "HIGH"
    elif report.full_scans or report.uses_temp_btree:
        report.cost_tier = "MEDIUM"
    else:
        report.cost_tier = "LOW"
    return report
