"""Offline tests for Business Data Investigator.

Place in project root (next to the app/ package), then run:
    python -m unittest -v test_investigator.py

Requires project dependencies, but NEVER calls an LLM or needs an API key.
Tests use an isolated temporary SQLite database and the expanded assignment fixture.
"""

import copy
from contextlib import closing
import importlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch


CUSTOMERS = [("C1", "small"), ("C2", "large"), ("C3", "small"), ("C4", "large")]
ORDERS = [
    ("O1", "C1", "2026-08-05", 5000),
    ("O2", "C2", "2026-08-10", 5000),
    ("O3", "C1", "2026-09-05", 5000),
    ("O4", "C2", "2026-09-10", 5000),
    ("O5", "C1", "2026-08-31", 4000),
    ("O6", "C3", "2026-08-15", 2000),
    ("O7", "C2", "2026-09-01", 6000),
    ("O8", "C3", "2026-09-30", 3000),
    ("O9", "C4", "2026-10-01", 2500),
]
REFUNDS = [
    ("RF1", "O1", "2026-08-20", 1000),
    ("RF2", "O3", "2026-09-20", 2000),
    ("RF3", "O3", "2026-09-21", 1000),
    ("RF4", "O5", "2026-09-02", 1500),
    ("RF5", "O5", "2026-09-03", 500),
    ("RF6", "O7", "2026-09-30", 1000),
    ("RF7", "O8", "2026-10-02", 1000),
    ("RF8", "O2", "2026-09-01", 700),
    ("RF9", "O9", "2026-10-05", 500),
]


class InvestigatorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="investigator-tests-")
        cls.db_path = Path(cls.temp.name) / "test.sqlite"
        with closing(sqlite3.connect(cls.db_path)) as conn, conn:
            conn.executescript("""
                CREATE TABLE customers (customer_id TEXT PRIMARY KEY, segment TEXT NOT NULL);
                CREATE TABLE orders (order_id TEXT PRIMARY KEY, customer_id TEXT NOT NULL,
                                     order_date TEXT NOT NULL, amount_cents INTEGER NOT NULL);
                CREATE TABLE refunds (refund_id TEXT PRIMARY KEY, order_id TEXT NOT NULL,
                                      refund_date TEXT NOT NULL, amount_cents INTEGER NOT NULL);
            """)
            conn.executemany("INSERT INTO customers VALUES (?, ?)", CUSTOMERS)
            conn.executemany("INSERT INTO orders VALUES (?, ?, ?, ?)", ORDERS)
            conn.executemany("INSERT INTO refunds VALUES (?, ?, ?, ?)", REFUNDS)

        cls.env = patch.dict(os.environ, {
            "INVESTIGATION_DB": str(cls.db_path),
            "DATABASE_PATH": str(cls.db_path),
            "DB_PATH": str(cls.db_path),
            "OPENAI_API_KEY": "",
        })
        cls.env.start()
        # The project must be importable as `app` from its root directory.
        cls.database = importlib.import_module("app.database")
        cls.agent_module = importlib.import_module("app.agent")
        cls.main_module = importlib.import_module("app.main")
        # Accommodate a project that reads DB_PATH at import time.
        for module in (cls.database, importlib.import_module("app.config")):
            if hasattr(module, "DB_PATH"):
                setattr(module, "DB_PATH", cls.db_path)

    @classmethod
    def tearDownClass(cls):
        cls.env.stop()
        cls.temp.cleanup()

    def sql(self, statement):
        result = self.database.query(statement)
        self.assertNotIn("error", result, f"SQL failed: {result}")
        return result

    def test_select_query(self):
        result = self.sql("SELECT order_id FROM orders")
        print(result)
        self.assertEqual(len(result["rows"]), 9)

    def test_reject_writes_and_preserve_data(self):
        before = self.sql("SELECT SUM(amount_cents) FROM orders")["rows"][0][0]
        for statement in (
            "DELETE FROM orders",
            "UPDATE orders SET amount_cents = 0",
            "DROP TABLE orders",
            "INSERT INTO customers VALUES ('BAD', 'small')",
        ):
            with self.subTest(sql=statement):
                self.assertIn("error", self.database.query(statement))
        after = self.sql("SELECT SUM(amount_cents) FROM orders")["rows"][0][0]
        self.assertEqual(before, after)
        self.assertEqual(
            len(self.sql("SELECT customer_id FROM customers")["rows"]),
            4
        )
    def test_invalid_sql(self):
        self.assertIn("error", self.database.query("SELECT * FROM no_such_table"))

    def test_multiple_statements_rejected(self):
        self.assertIn("error", self.database.query("SELECT 1; DELETE FROM orders"))

    def test_monthly_financial_totals(self):
        result = self.sql("""
            WITH gross AS (
                SELECT substr(order_date, 1, 7) AS month, SUM(amount_cents) AS gross
                FROM orders GROUP BY 1
            ), refunded AS (
                SELECT substr(refund_date, 1, 7) AS month, SUM(amount_cents) AS refunds
                FROM refunds GROUP BY 1
            )
            SELECT gross.month, gross.gross, COALESCE(refunded.refunds, 0),
                   gross.gross - COALESCE(refunded.refunds, 0)
            FROM gross LEFT JOIN refunded ON gross.month = refunded.month
            ORDER BY gross.month
        """)
        self.assertEqual(result["rows"], [
            ["2026-08", 16000, 1000, 15000],
            ["2026-09", 19000, 6700, 12300],
            ["2026-10", 2500, 1500, 1000],
        ])

    def test_multiple_refunds_per_order(self):
        result = self.sql("""
            SELECT order_id, COUNT(*), SUM(amount_cents)
            FROM refunds GROUP BY order_id HAVING COUNT(*) > 1 ORDER BY order_id
        """)
        self.assertEqual(result["rows"], [["O3", 2, 3000], ["O5", 2, 2000]])

    def test_cross_month_refunds(self):
        result = self.sql("""
            SELECT r.refund_id FROM refunds r JOIN orders o ON o.order_id = r.order_id
            WHERE substr(r.refund_date, 1, 7) <> substr(o.order_date, 1, 7)
            ORDER BY r.refund_id
        """)
        self.assertEqual([r[0] for r in result["rows"]], ["RF4", "RF5", "RF7", "RF8"])

    def test_september_segment_totals(self):
        result = self.sql("""
            WITH gross AS (
                SELECT c.segment, SUM(o.amount_cents) AS amount FROM orders o
                JOIN customers c ON c.customer_id = o.customer_id
                WHERE o.order_date >= '2026-09-01' AND o.order_date < '2026-10-01'
                GROUP BY c.segment
            ), refunded AS (
                SELECT c.segment, SUM(r.amount_cents) AS amount FROM refunds r
                JOIN orders o ON o.order_id = r.order_id
                JOIN customers c ON c.customer_id = o.customer_id
                WHERE r.refund_date >= '2026-09-01' AND r.refund_date < '2026-10-01'
                GROUP BY c.segment
            )
            SELECT gross.segment, gross.amount, COALESCE(refunded.amount, 0),
                   gross.amount - COALESCE(refunded.amount, 0)
            FROM gross LEFT JOIN refunded ON gross.segment = refunded.segment
            ORDER BY gross.segment
        """)
        self.assertEqual(result["rows"], [["large", 11000, 1700, 9300], ["small", 8000, 5000, 3000]])

    def test_offline_without_llm(self):
        with patch.object(self.agent_module, "ChatOpenAI", side_effect=AssertionError("LLM called")):
            agent = self.agent_module.InvestigatorAgent()
            answer, attempts = agent.offline_investigate("Show sales")
        self.assertIn("Offline", answer)
        self.assertEqual(len(attempts), 2)
        self.assertTrue(all("error" not in a["result"] for a in attempts))

    def make_saved_turn(self):
        from langchain_core.messages import AIMessage
        sqls = [
            "SELECT substr(order_date, 1, 7) AS month, SUM(amount_cents) AS gross FROM orders GROUP BY 1 ORDER BY 1",
            "SELECT substr(refund_date, 1, 7) AS month, SUM(amount_cents) AS refunded FROM refunds GROUP BY 1 ORDER BY 1",
        ]
        messages = []
        attempts = []
        for index, statement in enumerate(sqls, 1):
            messages.append(AIMessage(content="", tool_calls=[{
                "name": "query_database", "args": {"sql": statement},
                "id": f"call_{index}", "type": "tool_call",
            }]).model_dump(mode="json"))
            attempts.append({"sql": statement, "result": self.sql(statement)})
        messages.append(AIMessage(content="Net sales fell by $27.").model_dump(mode="json"))
        return {
            "question": "Compare net sales in August and September",
            "mode": "model",
            "answer": "Net sales fell by $27.",
            "model_answer_unverified": "Net sales fell by $27.",
            "model_responses": messages,
            "attempts": attempts,
        }

    def test_replay_without_api_key(self):
        saved = self.make_saved_turn()
        with patch.object(self.agent_module, "ChatOpenAI", side_effect=AssertionError("LLM called")):
            agent = self.agent_module.InvestigatorAgent()
            actual_attempts = []
            answer, verification = agent.replay_investigate(saved, actual_attempts)
        self.assertEqual(answer, saved["answer"])
        self.assertTrue(verification["matched"])
        self.assertEqual(verification["checked_queries"], 2)
        self.assertEqual(len(actual_attempts), 2)

    def test_replay_detects_changed_evidence(self):
        saved = copy.deepcopy(self.make_saved_turn())
        saved["attempts"][0]["result"]["rows"][0][1] += 1
        agent = self.agent_module.InvestigatorAgent()
        _, verification = agent.replay_investigate(saved, [])
        self.assertFalse(verification["matched"])
        self.assertEqual(verification["mismatch_queries"], [1])

    def test_replay_rejects_sql_tampering(self):
        saved = self.make_saved_turn()
        saved["attempts"][0]["sql"] = "SELECT 1"
        with self.assertRaises(ValueError):
            self.agent_module.InvestigatorAgent().replay_investigate(saved, [])

    def test_replay_api(self):
        from fastapi.testclient import TestClient
        client = TestClient(self.main_module.app)
        with patch.object(self.agent_module, "ChatOpenAI", side_effect=AssertionError("LLM called")):
            response = client.post("/investigations/replay", json={
                "export_data": {"turns": [self.make_saved_turn()]}, "turn_index": 0
            })
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["turn"]["replay_verification"]["matched"])
        self.assertEqual(response.json()["turn"]["mode"], "replay")

    def test_replay_api_rejects_offline_turn(self):
        from fastapi.testclient import TestClient
        response = TestClient(self.main_module.app).post("/investigations/replay", json={
            "export_data": {"turns": [{"mode": "offline"}]}, "turn_index": 0
        })
        self.assertEqual(response.status_code, 422)


if __name__ == "__main__":
    unittest.main(verbosity=2)
