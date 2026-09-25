"""Adversarial unit tests for the no-destructive-queries guardrail."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from guardrail import validate


class TestAllowedSelects(unittest.TestCase):
    def test_plain_select(self):
        self.assertTrue(validate("SELECT * FROM orders").ok)

    def test_lowercase_select(self):
        self.assertTrue(validate("select count(*) from customers").ok)

    def test_mixed_case(self):
        self.assertTrue(validate("SeLeCt * FrOm orders").ok)

    def test_with_cte(self):
        self.assertTrue(
            validate("WITH rev AS (SELECT campaign_id, SUM(amount) r FROM orders "
                     "GROUP BY 1) SELECT * FROM rev").ok)

    def test_keyword_inside_string_literal(self):
        # 'Delete me' is a filter value, not a command.
        self.assertTrue(
            validate("SELECT * FROM customers WHERE full_name = 'Delete me'").ok)

    def test_quoted_destructive_word_as_value(self):
        self.assertTrue(validate("SELECT 'DROP TABLE x' AS joke").ok)

    def test_trailing_semicolon_only(self):
        self.assertTrue(validate("select * from orders;").ok)

    def test_trailing_line_comment(self):
        self.assertTrue(validate("SELECT 1; -- trailing comment").ok)

    def test_semicolon_inside_string(self):
        self.assertTrue(
            validate("select * from t where name = 'it''s a ; trick'").ok)

    def test_line_comment_between_clauses(self):
        self.assertTrue(
            validate("SELECT * -- fetch everything\nFROM orders").ok)


class TestBlockedDestructive(unittest.TestCase):
    def assertBlocked(self, sql, needle=None):
        d = validate(sql)
        self.assertFalse(d.ok, f"expected refusal for: {sql!r}")
        if needle:
            self.assertIn(needle, d.reason.upper())

    def test_delete(self):
        self.assertBlocked("DELETE FROM orders", "DELETE")

    def test_drop(self):
        self.assertBlocked("DROP TABLE customers", "DROP")

    def test_update(self):
        self.assertBlocked("UPDATE customers SET city='X'", "UPDATE")

    def test_insert(self):
        self.assertBlocked(
            "INSERT INTO orders VALUES (1,1,1,'2026-01-01','Home',10)", "INSERT")

    def test_alter(self):
        self.assertBlocked("ALTER TABLE orders ADD COLUMN x TEXT", "ALTER")

    def test_create(self):
        self.assertBlocked("CREATE INDEX i ON orders(amount)", "CREATE")

    def test_truncate(self):
        self.assertBlocked("TRUNCATE TABLE orders", "TRUNCATE")

    def test_pragma(self):
        self.assertBlocked("PRAGMA table_info(orders)", "PRAGMA")

    def test_attach(self):
        self.assertBlocked("ATTACH DATABASE '/tmp/x.db' AS x", "ATTACH")

    def test_vacuum(self):
        self.assertBlocked("VACUUM", "VACUUM")


class TestStackedAndObfuscated(unittest.TestCase):
    def test_stacked_drop(self):
        d = validate("SELECT * FROM orders; DROP TABLE customers")
        self.assertFalse(d.ok)
        self.assertIn("MULTIPLE STATEMENTS", d.reason.upper())

    def test_two_selects(self):
        self.assertFalse(validate("SELECT 1; SELECT 2").ok)

    def test_comment_obfuscation(self):
        # DR/**/OP collapses to DROP after comment stripping.
        d = validate("DR/**/OP TABLE customers")
        self.assertFalse(d.ok)
        self.assertIn("DROP", d.reason.upper())

    def test_delete_behind_block_comment(self):
        self.assertFalse(validate("/* sneaky */ DELETE FROM orders").ok)

    def test_delete_inside_with(self):
        self.assertFalse(
            validate("WITH x AS (SELECT 1) DELETE FROM orders").ok)

    def test_union_select_still_allowed(self):
        # UNION is a read-only composition, not a stacked query.
        self.assertTrue(
            validate("SELECT city FROM customers UNION "
                     "SELECT channel FROM campaigns").ok)


class TestEmpty(unittest.TestCase):
    def test_empty(self):
        self.assertFalse(validate("").ok)

    def test_whitespace(self):
        self.assertFalse(validate("   \n  ").ok)

    def test_only_comment(self):
        self.assertFalse(validate("-- nothing here").ok)


if __name__ == "__main__":
    unittest.main()
