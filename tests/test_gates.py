import unittest
import os
os.environ["TOTAL_BUDGET"] = "75.00"
import shutil
import sqlite3
from council.gates import CodebaseGraphManager, GateCascade
from council.escalation import CompletionGate, CleanRoomEscalator
from council.ledger import Ledger
from council.client import UnifiedClient

class TestGatesAndEscalation(unittest.TestCase):
    
    def setUp(self):
        os.environ["TOTAL_BUDGET"] = "75.00"
        self.test_dir = "test_workspace_gates"
        if os.path.exists(self.test_dir):
            shutil.rmtree(self.test_dir)
        os.makedirs(self.test_dir)

        # Setup ledger, client, and graph managers in sandbox
        self.graph_manager = CodebaseGraphManager(workspace_path=self.test_dir)
        self.gate_cascade = GateCascade(self.graph_manager)
        self.completion_gate = CompletionGate(self.test_dir)
        
        self.ledger = Ledger(filepath=os.path.join(self.test_dir, "test_ledger.json"))
        self.client = UnifiedClient(self.ledger)
        self.escalator = CleanRoomEscalator(self.client)

    def tearDown(self):
        if os.path.exists(self.test_dir):
            shutil.rmtree(self.test_dir)

    def test_codebase_graph_reachability(self):
        # 1. Setup mock codebase structure
        # Schema files (tagged as high-stakes)
        self.graph_manager.add_node("db_schema.py", is_tagged=True)
        # Controller depends on Schema
        self.graph_manager.add_node("controller.py", is_tagged=False)
        self.graph_manager.add_edge("controller.py", "db_schema.py", dep_type="import")
        # Router depends on Controller
        self.graph_manager.add_node("router.py", is_tagged=False)
        self.graph_manager.add_edge("router.py", "controller.py", dep_type="import")
        # Utils (independent)
        self.graph_manager.add_node("utils.py", is_tagged=False)

        # 2. Run reachability precomputing
        self.graph_manager.precompute_reachability(k_hops=2)

        # Verify precompute boolean columns
        conn = sqlite3.connect(self.graph_manager.db_path)
        cursor = conn.cursor()
        
        cursor.execute("SELECT filename, is_tagged, reaches_tagged FROM nodes")
        rows = {row[0]: (row[1], row[2]) for row in cursor.fetchall()}
        conn.close()

        # Schema is tagged
        self.assertEqual(rows["db_schema.py"], (1, 1))
        # Controller (1 hop away) reaches tagged
        self.assertEqual(rows["controller.py"], (0, 1))
        # Router (2 hops away) reaches tagged
        self.assertEqual(rows["router.py"], (0, 1))
        # Utils does not reach tagged
        self.assertEqual(rows["utils.py"], (0, 0))

        # 3. Verify touch-list checks
        # Case A: Touches independent file -> False
        self.assertFalse(self.graph_manager.check_touch_list(["utils.py"]))
        # Case B: Touches schema (tagged) -> True
        self.assertTrue(self.graph_manager.check_touch_list(["db_schema.py"]))
        # Case C: Touches router (reaches schema within 2 hops) -> True
        self.assertTrue(self.graph_manager.check_touch_list(["router.py"]))
        # Case D: Touches newly added/unindexed file -> True (fails safe to true)
        self.assertTrue(self.graph_manager.check_touch_list(["new_secret_file.py"]))

    def test_gate_cascade_routing(self):
        # Seed graph so check_touch_list returns false unless specified
        self.graph_manager.add_node("utils.py", is_tagged=False)

        # Gate 0 Override:
        self.assertEqual(self.gate_cascade.route("!think write some code"), "thinker")
        self.assertEqual(self.gate_cascade.route("!cheap write some code"), "explorer")

        # Gate 1 Stakes (Keywords / Security):
        self.assertEqual(self.gate_cascade.route("Fix auth credentials leak in deployment"), "thinker")
        self.assertEqual(self.gate_cascade.route("modify the database schema"), "thinker")

        # Gate 1 Stakes (Touch list):
        self.graph_manager.add_node("db_schema.py", is_tagged=True)
        self.assertEqual(self.gate_cascade.route("ordinary change", modified_files=["db_schema.py"]), "thinker")

        # Gate 2 Dependency / Accumulation:
        # Prompt with complex keywords
        self.assertEqual(self.gate_cascade.route("reconcile trade-off dependencies across controllers"), "thinker")
        # Decision accumulation threshold >= 5
        self.assertEqual(self.gate_cascade.route("minor tweak", decision_accumulation=5), "thinker")
        self.assertEqual(self.gate_cascade.route("minor tweak", decision_accumulation=3), "explorer")

        # Gate 3 Volume:
        self.assertEqual(self.gate_cascade.route("summarize this long transcript file"), "explorer")
        self.assertEqual(self.gate_cascade.route("draft a layout blueprint"), "explorer")

        # Gate 4 Everything Else (Fallback):
        self.assertEqual(self.gate_cascade.route("Write a standard binary search function in Python"), "explorer")

    def test_file_based_completion_gates(self):
        task_id = "task-verify-123"
        items = ["Implement auth handler", "Add unit tests", "Deploy to stage"]

        # Create completion gate checklist -> returns verify token (FR-15a)
        token = self.completion_gate.create_gate(task_id, items)

        # Verify write-lockout (PermissionError on recreation)
        with self.assertRaises(PermissionError):
            self.completion_gate.create_gate(task_id, items)

        # Check completeness status -> False
        self.assertFalse(self.completion_gate.is_complete(task_id))

        # Executor without the token cannot check off its own criteria
        with self.assertRaises(PermissionError):
            self.completion_gate.check_off_item(task_id, "Implement auth handler", "wrong-token")

        # Check off items one by one with the runner's token
        self.completion_gate.check_off_item(task_id, "Implement auth handler", token)
        self.assertFalse(self.completion_gate.is_complete(task_id))

        self.completion_gate.check_off_item(task_id, "Add unit tests", token)
        self.completion_gate.check_off_item(task_id, "Deploy to stage", token)

        # Now all items are checked off -> True
        self.assertTrue(self.completion_gate.is_complete(task_id))

    def test_clean_room_escalation_payload(self):
        task_id = "task-escalate-abc"
        original_prompt = "Implement a safe auth module."
        pre_attempt_snapshot = "INVARIANTS: Always encode passwords using bcrypt."
        failed_output = "def auth(pwd):\n    return pwd == 'admin'" # flawed code

        # Run simulated escalation
        response = self.escalator.package_and_escalate(
            task_id=task_id,
            original_prompt=original_prompt,
            pre_attempt_snapshot=pre_attempt_snapshot,
            failed_output=failed_output
        )

        # Verify the client recorded the transaction under the Thinker tier
        transactions = self.ledger.data["tasks"][task_id]["transactions"]
        self.assertEqual(len(transactions), 1)
        # Thinker tier resolves via the config-driven tier map (models.json)
        self.assertEqual(transactions[0]["model"], self.client.select_model("thinker", "anthropic"))
        self.assertIn("Thinker Response", response)

if __name__ == "__main__":
    unittest.main()
