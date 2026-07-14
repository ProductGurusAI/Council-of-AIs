import unittest
import os
os.environ["TOTAL_BUDGET"] = "75.00"
import json
import time
import shutil
import threading
from council.ledger import Ledger

class TestLedgerReservations(unittest.TestCase):
    def setUp(self):
        os.environ["TOTAL_BUDGET"] = "75.00"
        self.test_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "test_workspace_reservations"))
        if os.path.exists(self.test_dir):
            shutil.rmtree(self.test_dir)
        os.makedirs(self.test_dir)
        
    def tearDown(self):
        if os.path.exists(self.test_dir):
            shutil.rmtree(self.test_dir)

    def test_json_migration_on_first_run(self):
        json_path = os.path.join(self.test_dir, "ledger_store.json")
        db_path = os.path.join(self.test_dir, "ledger_store.db")
        
        legacy_data = {
            "total_budget": 50.00,
            "reserve_floor": 10.00,
            "per_task_cap": 3.00,
            "total_spent": 1.25,
            "tasks": {
                "task-123": {
                    "total_spent": 1.25,
                    "transactions": [
                        {
                            "provider": "anthropic",
                            "model": "claude-sonnet-5",
                            "input_tokens": 100,
                            "output_tokens": 200,
                            "cache_write_tokens": 0,
                            "cache_read_tokens": 0,
                            "cost": 1.25
                        }
                    ],
                    "loop_history": ["init", "explore"]
                }
            }
        }
        with open(json_path, "w") as f:
            json.dump(legacy_data, f)
        old_env_budget = os.environ.pop("TOTAL_BUDGET", None)
        try:
            ledger = Ledger(filepath=json_path, pricing_config=None)
        finally:
            if old_env_budget is not None:
                os.environ["TOTAL_BUDGET"] = old_env_budget
        
        self.assertTrue(os.path.exists(db_path))
        self.assertTrue(os.path.exists(json_path + ".migrated"))
        
        self.assertEqual(ledger.data["total_budget"], 50.00)
        self.assertEqual(ledger.data["total_spent"], 1.25)
        
        tasks = ledger.data["tasks"]
        self.assertIn("task-123", tasks)
        self.assertEqual(tasks["task-123"]["total_spent"], 1.25)
        self.assertEqual(len(tasks["task-123"]["transactions"]), 1)
        self.assertEqual(tasks["task-123"]["loop_history"], ["init", "explore"])

    def test_reservation_auto_expiration(self):
        db_path = os.path.join(self.test_dir, "ledger_store.db")
        ledger = Ledger(filepath=db_path, total_budget=15.00, reserve_floor=0.0, per_task_cap=15.00, pricing_config=None)
        
        res_id = ledger.reserve("task-1", 12.00)
        
        with self.assertRaises(PermissionError):
            ledger.reserve("task-2", 5.00)
            
        import sqlite3
        conn = sqlite3.connect(ledger.db_path)
        cursor = conn.cursor()
        cursor.execute("UPDATE reservations SET created_at = ? WHERE id = ?", (time.time() - 650, res_id))
        conn.commit()
        conn.close()
        
        res_id_2 = ledger.reserve("task-2", 5.00)
        self.assertTrue(res_id_2.startswith("res-"))

    def test_adversarial_concurrency_race_condition(self):
        db_path = os.path.join(self.test_dir, "ledger_store.db")
        ledger = Ledger(filepath=db_path, total_budget=1.00, reserve_floor=0.0, per_task_cap=1.00, pricing_config=None)
        
        results = []
        errors = []
        
        def run_reserve():
            try:
                res_id = ledger.reserve("task-concurrent", 0.80)
                results.append(res_id)
            except PermissionError as pe:
                errors.append(pe)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=run_reserve) for _ in range(2)]
        
        for t in threads:
            t.start()
        for t in threads:
            t.join()
            
        self.assertEqual(len(results), 1)
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], PermissionError)

if __name__ == "__main__":
    unittest.main()
