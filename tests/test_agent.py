"""End-to-end pipeline tests: NL question -> SQL -> guardrail -> cost -> rows."""
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent
import guardrail
import llm
import make_db


class TestAgentPipeline(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="sqlguard_agent_")
        cls.db = os.path.join(cls.tmp, "t.db")
        make_db.build(cls.db)
        # Force the offline heuristic path even if env vars exist.
        cls._patch = mock.patch.object(llm, "is_configured",
                                       return_value=False)
        cls._patch.start()

    @classmethod
    def tearDownClass(cls):
        cls._patch.stop()

    def test_customer_count(self):
        ans = agent.ask(self.db, "How many customers do we have?")
        self.assertTrue(ans.ok)
        self.assertEqual(ans.rows, [(200,)])
        self.assertEqual(ans.backend, "heuristic")

    def test_total_revenue_matches_direct_sql(self):
        ans = agent.ask(self.db, "What is our total revenue?")
        conn = agent.connect(self.db)
        expected = conn.execute("SELECT ROUND(SUM(amount), 2) FROM orders"
                                ).fetchone()[0]
        conn.close()
        self.assertTrue(ans.ok)
        self.assertEqual(ans.rows[0][0], expected)

    def test_revenue_per_campaign_runs(self):
        ans = agent.ask(self.db, "What is the revenue per campaign?")
        self.assertTrue(ans.ok)
        self.assertEqual(ans.row_count, 12)
        self.assertIn("JOIN", ans.sql)

    def test_roi_query_runs(self):
        ans = agent.ask(self.db, "Which marketing channel has the best ROI?")
        self.assertTrue(ans.ok)
        self.assertEqual(ans.row_count, 4)

    def test_orders_in_month(self):
        ans = agent.ask(self.db, "How many orders were placed in June 2026?")
        conn = agent.connect(self.db)
        expected = conn.execute(
            "SELECT COUNT(*) FROM orders "
            "WHERE strftime('%Y-%m', order_date) = '2026-06'").fetchone()[0]
        conn.close()
        self.assertTrue(ans.ok)
        self.assertEqual(ans.rows[0][0], expected)

    def test_customers_from_city(self):
        ans = agent.ask(self.db, "List all customers from Austin.")
        self.assertTrue(ans.ok)
        self.assertGreater(ans.row_count, 0)
        self.assertTrue(all(r[1] == "Austin" for r in ans.rows))

    def test_refuse_delete(self):
        ans = agent.ask(self.db, "Delete all orders from last year.")
        self.assertTrue(ans.refused)
        self.assertFalse(ans.ok)
        self.assertIn("read-only", ans.message)

    def test_refuse_drop(self):
        ans = agent.ask(self.db, "Drop the customers table.")
        self.assertTrue(ans.refused)

    def test_refuse_stacked_intent(self):
        ans = agent.ask(
            self.db, "Show me all orders and then delete the customers table.")
        self.assertTrue(ans.refused)

    def test_soft_destructive_without_target_is_not_a_refusal(self):
        # "update me on revenue" is a status request, not a destructive op.
        ans = agent.ask(self.db, "update me on revenue")
        self.assertFalse(ans.refused)

    def test_unmapped_question_is_not_a_refusal(self):
        ans = agent.ask(self.db, "Tell me a joke about databases.")
        self.assertFalse(ans.ok)
        self.assertFalse(ans.refused)
        self.assertIn("schema", ans.message)

    def test_cost_report_attached(self):
        ans = agent.ask(self.db, "What is our total revenue?")
        self.assertIsNotNone(ans.cost)
        self.assertIn(ans.cost.cost_tier, ("LOW", "MEDIUM", "HIGH"))
        self.assertGreaterEqual(ans.elapsed_ms, 0)

    def test_connection_is_read_only(self):
        conn = agent.connect(self.db)
        with self.assertRaises(sqlite3.OperationalError):
            conn.execute("CREATE TABLE evil(x INT)")
        conn.close()

    def test_guardrail_gates_even_hand_written_sql(self):
        # Defense in depth: validate() itself blocks a destructive string.
        self.assertFalse(guardrail.validate("DELETE FROM orders").ok)

    def test_dataset_shape(self):
        conn = agent.connect(self.db)
        counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                  for t in ("customers", "campaigns", "orders")}
        conn.close()
        self.assertEqual(counts,
                         {"customers": 200, "campaigns": 12, "orders": 1500})

    def test_build_is_deterministic(self):
        db2 = os.path.join(self.tmp, "t2.db")
        make_db.build(db2)
        c1 = agent.connect(self.db)
        c2 = agent.connect(db2)
        r1 = c1.execute("SELECT SUM(amount) FROM orders").fetchone()[0]
        r2 = c2.execute("SELECT SUM(amount) FROM orders").fetchone()[0]
        c1.close(); c2.close()
        self.assertEqual(r1, r2)

    def test_format_answer_renders(self):
        ans = agent.ask(self.db, "How many customers do we have?")
        text = agent.format_answer(ans)
        self.assertIn("Q:", text)
        self.assertIn("SQL [heuristic]", text)
        self.assertIn("200", text)


class TestLLMBackend(unittest.TestCase):
    def test_extract_sql_from_fence(self):
        text = "Here you go:\n```sql\nSELECT 1\n```"
        self.assertEqual(llm.extract_sql(text), "SELECT 1")

    def test_extract_sql_raw(self):
        self.assertEqual(llm.extract_sql("SELECT 2;"), "SELECT 2")

    def test_not_configured_without_env(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("NL_SQL_LLM_ENDPOINT", None)
            os.environ.pop("NL_SQL_LLM_API_KEY", None)
            self.assertFalse(llm.is_configured())

    def test_generate_raises_when_unconfigured(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("NL_SQL_LLM_ENDPOINT", None)
            os.environ.pop("NL_SQL_LLM_API_KEY", None)
            with self.assertRaises(RuntimeError):
                llm.generate("hello")

    def test_generate_parses_mocked_response(self):
        payload = {"choices": [{"message": {"content":
                   "```sql\nSELECT COUNT(*) FROM customers\n```"}}]}

        class FakeResp:
            def read(self):
                import json as _json
                return _json.dumps(payload).encode()
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False

        env = {"NL_SQL_LLM_ENDPOINT": "https://example.invalid/v1/chat",
               "NL_SQL_LLM_API_KEY": "test-key-not-real"}
        with mock.patch.dict(os.environ, env, clear=False), \
             mock.patch("urllib.request.urlopen", return_value=FakeResp()):
            sql = llm.generate("how many customers?")
        self.assertEqual(sql, "SELECT COUNT(*) FROM customers")


if __name__ == "__main__":
    unittest.main()
