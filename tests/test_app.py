import gc
import io
import json
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import app


ROOT = Path(__file__).resolve().parents[1]
EXPECTED = json.loads((ROOT / "tasks/data/expected-seed-results.json").read_text())


class AppTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(gc.collect)
        self.db = Path(self.temp.name) / "seed.sqlite"
        shutil.copyfile(ROOT / "tasks/data/seed.sqlite", self.db)
        self.db_patch = patch.object(app, "DB_PATH", self.db)
        self.db_patch.start()
        self.addCleanup(self.db_patch.stop)
        self.state_patch = patch.dict(app.investigations, {}, clear=True)
        self.state_patch.start()
        self.addCleanup(self.state_patch.stop)
        self.client = TestClient(app.app)
        self.addCleanup(self.client.close)

    def investigate(self, question="Compare August and September net sales", **kwargs):
        response = self.client.post(
            "/investigations", json={"question": question, "mode": "offline", **kwargs}
        )
        self.assertEqual(response.status_code, 200)
        return response.json()

    def test_five_seed_demonstration_checks(self):
        turn = self.investigate()["turn"]
        self.assertIsNone(turn["error"])
        self.assertEqual(len(turn["attempts"]), 2)
        monthly = {row[0]: row[1:] for row in turn["attempts"][0]["result"]["rows"]}
        august, september = EXPECTED["periods"]
        checks = [
            ("August gross", monthly["2026-08"][0], august["gross_cents"]),
            ("August refunds", monthly["2026-08"][1], august["refunds_cents"]),
            ("August net", monthly["2026-08"][2], august["net_cents"]),
            ("September totals", monthly["2026-09"], [september["gross_cents"], september["refunds_cents"], september["net_cents"]]),
            ("Net change", monthly["2026-09"][2] - monthly["2026-08"][2], EXPECTED["net_change_cents"]),
        ]
        for label, actual, expected in checks:
            with self.subTest(check=label):
                self.assertEqual(actual, expected)
        self.assertIn("not an answer tailored", turn["answer"])
        self.assertIn("Refund reasons are not available", turn["answer"])
        self.assertFalse(turn["attempts"][0]["result"]["truncated"])

    def test_export_follow_up_and_segment_reconciliation(self):
        first = self.investigate()
        investigation_id = first["investigation_id"]
        second = self.investigate("Break down by segment", investigation_id=investigation_id)
        response = self.client.get(f"/investigations/{investigation_id}/export")
        self.assertEqual(response.status_code, 200)
        self.assertIn("attachment", response.headers["content-disposition"])
        export = response.json()
        self.assertEqual(export["turns"], [first["turn"], second["turn"]])
        self.assertEqual(export["definitions"], app.SYSTEM)
        self.assertEqual(export["configuration"]["query_limit"], app.MAX_TOOL_CALLS)
        self.assertFalse(export["in_progress"])
        monthly = {row[0]: row[1:] for row in export["turns"][1]["attempts"][0]["result"]["rows"]}
        segments = export["turns"][1]["attempts"][1]["result"]["rows"]
        for month, totals in monthly.items():
            rows = [row for row in segments if row[0] == month]
            self.assertEqual([sum(row[i] for row in rows) for i in range(2, 5)], totals)
        september = {row[1]: row[2:] for row in segments if row[0] == "2026-09"}
        self.assertEqual(september, {
            name: [values["gross_cents"], values["refunds_cents"], values["net_cents"]]
            for name, values in EXPECTED["september_segments"].items()
        })

    def test_write_denied_and_seed_unchanged(self):
        before = app.query("SELECT * FROM orders ORDER BY order_id")
        for sql in ("DELETE FROM orders", "UPDATE orders SET amount_cents = 0", "CREATE TABLE evil (x)"):
            with self.subTest(sql=sql):
                self.assertIn("error", app.query(sql))
        self.assertEqual(app.query("SELECT * FROM orders ORDER BY order_id"), before)

    def test_failed_attempts_are_retained_and_exported(self):
        failure = {"error": "deliberate query failure"}
        with patch.object(app, "query", side_effect=[failure, failure]):
            result = self.investigate()
        turn = result["turn"]
        self.assertEqual(turn["error"], "One or more offline queries failed.")
        self.assertIn("deliberate query failure", turn["answer"])
        self.assertEqual(len(turn["attempts"]), 2)
        for attempt in turn["attempts"]:
            self.assertTrue(attempt["sql"])
            self.assertEqual(attempt["result"], failure)
        exported = self.client.get(f"/investigations/{result['investigation_id']}/export").json()
        self.assertEqual(exported["turns"], [turn])

    def test_authorizer_denies_metadata_functions_and_attachments(self):
        self.assertEqual(app.query("SELECT count(customer_id) FROM customers")["rows"], [[2]])
        for sql in (
            "SELECT * FROM sqlite_master",
            "PRAGMA table_info(orders)",
            "SELECT load_extension('anything')",
            "SELECT readfile('anything')",
            "SELECT writefile('anything', 'data')",
            "ATTACH DATABASE ':memory:' AS other",
        ):
            with self.subTest(sql=sql):
                self.assertIn("error", app.query(sql))

    def test_authorizer_blocks_other_tables_and_dangerous_registered_functions(self):
        with sqlite3.connect(self.db) as conn:
            conn.execute("CREATE TABLE private_data (secret TEXT)")
            conn.execute("INSERT INTO private_data VALUES ('secret')")
        conn.close()
        self.assertIn("error", app.query("SELECT * FROM private_data"))
        real_connect = sqlite3.connect

        def connect_with_functions(*args, **kwargs):
            conn = real_connect(*args, **kwargs)
            for name in ("readfile", "writefile", "load_extension"):
                conn.create_function(name, 1, lambda value: value)
            return conn

        with patch.object(app.sqlite3, "connect", side_effect=connect_with_functions):
            for name in ("readfile", "writefile", "load_extension"):
                with self.subTest(function=name):
                    result = app.query(f"SELECT {name}('x')")
                    self.assertIn("error", result)
                    self.assertIn("not authorized", result["error"].lower())

    def test_ollama_tool_calls_and_saved_responses(self):
        replies = [
            {"message": {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "query_database", "arguments": {"sql": app.MONTHLY_SQL}}}]}},
            {"message": {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "query_database", "arguments": {"sql": app.SEGMENT_SQL}}}]}},
            {"message": {"role": "assistant", "content": "Gross and refunds were checked."}},
        ]
        requests = []

        def respond(request, timeout):
            requests.append(json.loads(request.data))
            return io.BytesIO(json.dumps(replies[len(requests) - 1]).encode())

        with patch.object(app, "urlopen", side_effect=respond):
            response = self.client.post("/investigations", json={"question": "Compare sales", "mode": "model"})
        turn = response.json()["turn"]
        self.assertIsNone(turn["error"])
        self.assertEqual(len(turn["attempts"]), 2)
        self.assertEqual(turn["model_responses"], replies)
        self.assertEqual(requests[1]["messages"][-1]["role"], "tool")
        self.assertEqual(requests[1]["messages"][-1]["tool_name"], "query_database")
        self.assertEqual(turn["attempts"][0]["result"]["rows"], [["2026-08", 10000, 1000, 9000], ["2026-09", 10000, 3000, 7000]])
        self.assertEqual(turn["attempts"][1]["sql"], app.SEGMENT_SQL)

    def test_model_generated_write_is_rejected(self):
        replies = [
            {"message": {"role": "assistant", "tool_calls": [{"function": {"name": "query_database", "arguments": {"sql": "DELETE FROM orders"}}}]}},
            {"message": {"role": "assistant", "tool_calls": [{"function": {"name": "query_database", "arguments": {"sql": "SELECT count(order_id) FROM orders"}}}]}},
            {"message": {"role": "assistant", "content": "Unable to complete."}},
        ]
        with patch.object(app, "urlopen", side_effect=[io.BytesIO(json.dumps(reply).encode()) for reply in replies]):
            turn = self.client.post("/investigations", json={"question": "Compare", "mode": "model"}).json()["turn"]
        self.assertIn("error", turn["attempts"][0]["result"])
        self.assertIn("Incomplete", turn["error"])
        self.assertEqual(app.query("SELECT count(order_id) FROM orders")["rows"], [[4]])

    def test_ollama_unavailable_is_visible(self):
        with patch.object(app, "urlopen", side_effect=OSError("Ollama unavailable")):
            response = self.client.post("/investigations", json={"question": "Compare sales", "mode": "model"})
        self.assertIn("Ollama unavailable", response.json()["turn"]["error"])


if __name__ == "__main__":
    unittest.main()
