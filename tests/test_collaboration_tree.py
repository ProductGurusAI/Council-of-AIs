import unittest
import os
import time
import shutil
import threading
from unittest.mock import MagicMock
from council.collaboration import CollaborationSession, _pending_user_inputs
from council.client import UnifiedClient
from council.memory import MemoryWrapper
from council.ledger import Ledger

class TestCollaborationTree(unittest.TestCase):
    def setUp(self):
        os.environ["TOTAL_BUDGET"] = "75.00"
        self.test_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "test_workspace_collab"))
        if os.path.exists(self.test_dir):
            shutil.rmtree(self.test_dir)
        os.makedirs(self.test_dir)
        self.memory = MemoryWrapper(workspace_path=self.test_dir)
        self.ledger = Ledger(filepath=os.path.join(self.test_dir, "ledger_store.json"), pricing_config=None)
        
        self.client = MagicMock(spec=UnifiedClient)
        self.client.is_simulated = True
        self.client.ledger = self.ledger
        self.client.select_model.side_effect = lambda tier, provider: f"mock-{provider}-{tier}"
        
    def tearDown(self):
        if os.path.exists(self.test_dir):
            shutil.rmtree(self.test_dir)

    def test_parallel_solution_tree_success(self):
        session = CollaborationSession(
            client=self.client,
            memory=self.memory,
            task_id="collab-tree-test",
            session_budget=2.00,
            mode="tree"
        )
        
        def mock_execute(task_id, tier, provider, cache_envelope, user_prompt, warm_cache, user_confirmed=False):
            if tier == "thinker":
                if "Choose/Merge:" in user_prompt:
                    return "SELECT B\nThis is much cleaner.", 0.01
                return "Contract: Interfaces...", 0.01
            elif tier == "explorer":
                if provider == "anthropic":
                    return "Branch A code...", 0.05
                elif provider == "openai":
                    return "Branch B code...", 0.05
                elif provider == "gemini":
                    return "Branch C code...", 0.05
            return "Default...", 0.01

        self.client.execute_task.side_effect = mock_execute
        
        result = session.run("Implement quicksort")
        
        self.assertEqual(result["status"], "approved")
        self.assertTrue(result["approved"])
        self.assertEqual(result["selected_branch"], "B")
        self.assertEqual(result["final_output"], "Branch B code...")

    def test_degrade_dont_die_adversarial_budget(self):
        self.ledger.data["total_budget"] = 0.11
        
        session = CollaborationSession(
            client=self.client,
            memory=self.memory,
            task_id="collab-tree-degrade",
            session_budget=2.00,
            mode="tree"
        )
        
        def mock_execute_degrade(task_id, tier, provider, cache_envelope, user_prompt, warm_cache, user_confirmed=False):
            if tier == "thinker":
                if "Choose/Merge:" in user_prompt:
                    return "SELECT A\nChoice.", 0.01
                return "Contract: Interfaces...", 0.01
            elif tier == "explorer":
                if provider == "anthropic":
                    return "Branch A code...", 0.05
                elif provider == "openai":
                    return "Branch B code...", 0.05
                elif provider == "gemini":
                    return "Branch C code...", 0.05
            return "Default...", 0.01

        self.client.execute_task.side_effect = mock_execute_degrade
        
        result = session.run("Implement sorting")
        
        self.assertEqual(result["status"], "approved")
        self.assertTrue(result["selected_branch"] in ("A", "B", "C"))
        self.assertEqual(result["final_output"], f"Branch {result['selected_branch']} code...")

    def test_interactive_user_pause_need_user(self):
        session = CollaborationSession(
            client=self.client,
            memory=self.memory,
            task_id="collab-user-pause",
            session_budget=2.00,
            mode="linear"
        )
        
        self.client.execute_task.side_effect = [
            ("NEED-USER: Should we support floats?\nContract details...", 0.01),
            ("Contract: Support floats...", 0.01),
            ("NONE", 0.01),
            ("Implementation code...", 0.05),
            ("APPROVE\nAll good.", 0.01)
        ]
        
        def answer_user():
            time.sleep(0.1)
            from council.dashboard_server import COLLAB_RESULTS
            for _ in range(20):
                if COLLAB_RESULTS.get("collab-user-pause", {}).get("status") == "awaiting_user":
                    break
                time.sleep(0.1)
                
            self.assertEqual(COLLAB_RESULTS["collab-user-pause"]["status"], "awaiting_user")
            self.assertIn("NEED-USER: Should we support floats?", COLLAB_RESULTS["collab-user-pause"]["question"])
            
            from council.collaboration import _pending_user_inputs
            self.assertIn("collab-user-pause", _pending_user_inputs)
            _pending_user_inputs["collab-user-pause"]["answer"] = "Yes, support floats"
            _pending_user_inputs["collab-user-pause"]["event"].set()

        from council.dashboard_server import COLLAB_RESULTS
        COLLAB_RESULTS["collab-user-pause"] = {"status": "starting"}
        
        t = threading.Thread(target=answer_user)
        t.start()
        
        result = session.run("Create decimal rounder")
        t.join()
        
        self.assertEqual(result["status"], "approved")
        self.assertTrue(result["approved"])
        self.assertEqual(result["final_output"], "Implementation code...")

if __name__ == "__main__":
    unittest.main()
