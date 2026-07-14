import unittest
import os
os.environ["TOTAL_BUDGET"] = "75.00"
import shutil
from council.ledger import Ledger
from council.client import UnifiedClient, CacheEnvelope
from council.bypass import BypassLane

class TestCouncilSpine(unittest.TestCase):
    
    def setUp(self):
        import shutil
        os.environ["TOTAL_BUDGET"] = "75.00"
        self.test_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "test_workspace_spine"))
        if os.path.exists(self.test_dir):
            shutil.rmtree(self.test_dir)
        os.makedirs(self.test_dir)
        
        self.test_ledger_file = os.path.join(self.test_dir, "test_ledger_store.json")
        self.ledger = Ledger(
            filepath=self.test_ledger_file,
            total_budget=75.00,
            reserve_floor=10.00,
            per_task_cap=3.00
        )
        self.client = UnifiedClient(self.ledger)
        self.bypass_lane = BypassLane()

    def tearDown(self):
        import shutil
        if os.path.exists(self.test_dir):
            shutil.rmtree(self.test_dir)
        if os.path.exists("tasks_store"):
            pass

    def test_ledger_cost_calculation(self):
        # Claude 3 Opus standard rates: Input $15/1M, Output $75/1M, cache_write $18.75/1M (1.25x), cache_read $1.50/1M (0.10x)
        # Scenario: 1,000,000 input, 10,000 output.
        cost = self.ledger.calculate_cost("claude-3-opus-20240229", 1000000, 10000)
        # Expected: (1,000,000 * 15.00 + 10,000 * 75.00) / 1000000 = 15.00 + 0.75 = 15.75
        self.assertAlmostEqual(cost, 15.75, places=4)

        # Caching: 1,000,000 cache read, 10,000 output, 100 volatile input
        cost_cached = self.ledger.calculate_cost(
            "claude-3-opus-20240229",
            input_tokens=100,
            output_tokens=10000,
            cache_read_tokens=1000000
        )
        # Expected: (100 * 15.00 + 10,000 * 75.00 + 1,000,000 * 1.50) / 1000000 = (1500 + 750000 + 1500000) / 1000000 = 2.2515
        self.assertAlmostEqual(cost_cached, 2.2515, places=4)

    def test_ledger_constraints(self):
        task_id = "test-task-123"
        
        # 1. Standard check within boundaries
        allowed, reason = self.ledger.check_constraints(task_id, 0.50, is_thinker_escalation=False)
        self.assertTrue(allowed)
        
        # 2. Exceeding Task Cap ($3.00)
        # Record $2.90 first
        self.ledger.record_spend(task_id, "anthropic", "gpt-4o", 580000, 0)
        # Verify task spend is $2.90
        self.assertAlmostEqual(self.ledger.get_task_spend(task_id), 2.90, places=4)
        
        # Try to spend another $0.20 (takes task spend to $3.10 > $3.00)
        allowed, reason = self.ledger.check_constraints(task_id, 0.20, is_thinker_escalation=False)
        self.assertFalse(allowed)
        self.assertIn("Per-task budget cap reached", reason)

        # 3. Reserve Floor Check ($10.00 floor on $75.00 budget)
        # Make total spend $66.00 (remaining budget = $9.00)
        # We record to a different task to not trigger the per-task cap on the test task
        # Total spent so far: 2.90. We record another 63.10 to make it 66.00 total spent.
        self.ledger.record_spend("other-task", "openai", "gpt-4o", 12620000, 0) # cost = 63.10
        # Total spent: 2.90 + 63.10 = 66.00. Remaining: 9.00
        self.assertAlmostEqual(self.ledger.get_remaining_budget(), 9.00, places=4)

        # Now try to run an Explorer task costing $0.50 (budget will go to $8.50, which is below $10.00 floor)
        # This is NOT a Thinker escalation, so it should be rejected.
        allowed, reason = self.ledger.check_constraints(task_id, 0.50, is_thinker_escalation=False)
        self.assertFalse(allowed)
        self.assertIn("Reserve floor threshold hit", reason)

        # If it IS a Thinker escalation, it should be allowed (with user_confirmed=True under endgame rules)!
        allowed, reason = self.ledger.check_constraints("thinker-task", 0.50, is_thinker_escalation=True, user_confirmed=True)
        self.assertTrue(allowed)

    def test_runaway_loop_protection(self):
        task_id = "runaway-task"
        
        # Record states: run-cheap -> run-cheap -> run-cheap
        self.ledger.record_state(task_id, "run-cheap")
        self.assertFalse(self.ledger.check_runaway_loop(task_id))
        
        self.ledger.record_state(task_id, "run-cheap")
        self.assertFalse(self.ledger.check_runaway_loop(task_id))
        
        self.ledger.record_state(task_id, "run-cheap")
        self.assertFalse(self.ledger.check_runaway_loop(task_id))
        
        # 4th repeat of same state triggers warning
        self.ledger.record_state(task_id, "run-cheap")
        self.assertTrue(self.ledger.check_runaway_loop(task_id))

    def test_bypass_lane_heuristics(self):
        # Simple greetings should bypass
        bypass, tier, cost = self.bypass_lane.classify("Hello there!")
        self.assertTrue(bypass)
        self.assertEqual(tier, "Tier 1 (Regex)")

        # Short question without keywords should bypass
        bypass, tier, cost = self.bypass_lane.classify("what is 25+14?")
        self.assertTrue(bypass)
        self.assertEqual(tier, "Tier 1 (Regex)")

        # Prompt with project keywords (schema, refactor) should NOT bypass
        bypass, tier, cost = self.bypass_lane.classify("Refactor the database schema for the users table.")
        self.assertFalse(bypass)
        # Should be caught by project keywords heuristic block
        self.assertEqual(tier, "Tier 1 (Project Block)")

if __name__ == "__main__":
    unittest.main()
