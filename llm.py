"""Optional LLM backend for SQL generation (OpenAI-compatible API).

The agent works fully offline with its built-in heuristic generator
(`agent.py`) — no key, no network. This module is only used when the user
opts in by setting environment variables:

    NL_SQL_LLM_ENDPOINT  e.g. https://api.openai.com/v1/chat/completions
    NL_SQL_LLM_API_KEY    the bearer token (never committed, never logged)
    NL_SQL_LLM_MODEL      default: gpt-4o-mini

Even LLM-produced SQL goes through the guardrail before it may run.

Stdlib only (urllib) — no extra dependency.
"""
from __future__ import annotations

import json
import os
import re
import urllib.request

DEFAULT_MODEL = "gpt-4o-mini"

SCHEMA_DESCRIPTION = """\
You generate read-only SQLite queries. Schema (all data fictional):

customers(customer_id INTEGER, full_name TEXT, city TEXT, signup_date TEXT)
campaigns(campaign_id INTEGER, campaign_name TEXT, channel TEXT,
          start_date TEXT, end_date TEXT, spend REAL)
orders(order_id INTEGER, customer_id INTEGER, campaign_id INTEGER,
       order_date TEXT, category TEXT, amount REAL)

Rules: output ONLY a single SELECT statement (WITH ... SELECT is fine).
Never emit DELETE, DROP, UPDATE, INSERT, ALTER, CREATE, or multiple
statements. Dates are ISO-8601 strings (YYYY-MM-DD).
"""

_FENCE_RE = re.compile(r"```(?:sql)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


def is_configured() -> bool:
    return bool(os.environ.get("NL_SQL_LLM_ENDPOINT")
                and os.environ.get("NL_SQL_LLM_API_KEY"))


def extract_sql(text: str) -> str:
    """Pull the SQL out of a model reply (fenced block or raw)."""
    m = _FENCE_RE.search(text or "")
    sql = m.group(1).strip() if m else (text or "").strip()
    return sql.rstrip(";").strip()


def generate(question: str, timeout: int = 30) -> str:
    """Ask the configured LLM for SQL. Raises RuntimeError if not configured."""
    endpoint = os.environ.get("NL_SQL_LLM_ENDPOINT")
    api_key = os.environ.get("NL_SQL_LLM_API_KEY")
    if not endpoint or not api_key:
        raise RuntimeError(
            "LLM backend not configured — set NL_SQL_LLM_ENDPOINT and "
            "NL_SQL_LLM_API_KEY to enable it."
        )
    model = os.environ.get("NL_SQL_LLM_MODEL", DEFAULT_MODEL)
    body = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": SCHEMA_DESCRIPTION},
            {"role": "user", "content": question},
        ],
        "temperature": 0,
    }).encode()
    req = urllib.request.Request(
        endpoint,
        data=body,
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {api_key}"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode())
    text = payload["choices"][0]["message"]["content"]
    return extract_sql(text)
