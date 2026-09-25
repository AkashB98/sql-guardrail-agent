"""Unit tests for the EXPLAIN QUERY PLAN cost estimator."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cost
import make_db


class TestCostEstimator(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="sqlguard_cost_")
        cls.db = os.path.join(cls.tmp, "t.db")
        make_db.build(cls.db)
        import sqlite3
        cls.conn = sqlite3.connect(cls.db)

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()

    def test_full_scan_detected(self):
        r = cost.estimate(self.conn, "SELECT * FROM orders")
        self.assertEqual(r.full_scans, ["orders"])
        self.assertEqual(r.estimated_rows_touched, 1500)

    def test_full_scan_large_table_is_high_tier(self):
        r = cost.estimate(self.conn, "SELECT * FROM orders")
        self.assertEqual(r.cost_tier, "HIGH")
        self.assertTrue(any("full-table scan" in w for w in r.warnings))

    def test_indexed_lookup_is_low_tier(self):
        r = cost.estimate(self.conn,
                          "SELECT * FROM orders WHERE order_id = 1")
        self.assertEqual(r.full_scans, [])
        self.assertIn("orders", r.indexed)
        self.assertEqual(r.cost_tier, "LOW")

    def test_indexed_city_filter(self):
        r = cost.estimate(self.conn,
                          "SELECT * FROM customers WHERE city = 'Austin'")
        self.assertEqual(r.full_scans, [])
        self.assertIn("customers", r.indexed)

    def test_cartesian_warning_and_product_bound(self):
        r = cost.estimate(self.conn, "SELECT * FROM customers, campaigns")
        self.assertEqual(r.cost_tier, "HIGH")
        self.assertTrue(any("cartesian" in w for w in r.warnings))
        self.assertEqual(r.estimated_rows_touched, 200 * 12)

    def test_indexed_join_no_cartesian_warning(self):
        r = cost.estimate(
            self.conn,
            "SELECT * FROM orders o JOIN campaigns c "
            "ON o.campaign_id = c.campaign_id")
        self.assertFalse(any("cartesian" in w for w in r.warnings))
        self.assertIn("campaigns", r.indexed)

    def test_temp_btree_flagged(self):
        r = cost.estimate(
            self.conn,
            "SELECT category, AVG(amount) FROM orders GROUP BY category")
        self.assertTrue(r.uses_temp_btree)

    def test_planner_error_does_not_raise(self):
        r = cost.estimate(self.conn, "SELECT * FROM no_such_table_xyz")
        self.assertEqual(r.cost_tier, "UNKNOWN")
        self.assertTrue(r.warnings)

    def test_estimate_is_deterministic(self):
        sql = "SELECT * FROM orders WHERE amount > 100"
        a = cost.estimate(self.conn, sql)
        b = cost.estimate(self.conn, sql)
        self.assertEqual((a.cost_tier, a.estimated_rows_touched,
                          sorted(a.warnings)),
                         (b.cost_tier, b.estimated_rows_touched,
                          sorted(b.warnings)))

    def test_scan_using_covering_index_counts_as_indexed(self):
        # EXPLAIN says "SCAN orders USING COVERING INDEX ..." — that is an
        # index path, not a full table scan. It must not vanish from the
        # report entirely (the original bug).
        r = cost.estimate(
            self.conn,
            "SELECT COUNT(*) FROM orders "
            "WHERE strftime('%Y-%m', order_date) = '2026-06'")
        self.assertEqual(r.full_scans, [])
        self.assertIn("orders", r.indexed)
        self.assertEqual(r.cost_tier, "LOW")

    def test_cte_names_are_not_reported_as_tables(self):
        r = cost.estimate(
            self.conn,
            "WITH rev AS (SELECT campaign_id, SUM(amount) AS revenue "
            "FROM orders GROUP BY campaign_id) "
            "SELECT c.channel FROM campaigns c "
            "LEFT JOIN rev ON rev.campaign_id = c.campaign_id "
            "GROUP BY c.channel")
        self.assertNotIn("rev", r.indexed)
        self.assertNotIn("rev", r.full_scans)
        self.assertIn("orders", r.indexed)
        self.assertIn("campaigns", r.full_scans)

    def test_summary_mentions_tier_and_rows(self):
        r = cost.estimate(self.conn, "SELECT * FROM orders")
        s = r.summary()
        self.assertIn("HIGH", s)
        self.assertIn("1,500", s)


if __name__ == "__main__":
    unittest.main()
