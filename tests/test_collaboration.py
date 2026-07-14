import os
import shutil
import unittest

from council.memory import MemoryWrapper
from council.collaboration import CollaborationSession


class StubClient:
    """
    Scriptable stand-in for UnifiedClient. Each call pops the next scripted
    response for the requested tier; costs are fixed per tier so budget-cap
    behavior is deterministic.
    """

    COSTS = {"thinker": 0.01, "explorer": 0.002}

    def __init__(self, thinker_script, explorer_script):
        self.scripts = {"thinker": list(thinker_script), "explorer": list(explorer_script)}
        self.calls = []
        self.is_simulated = True

    def execute_task(self, task_id, tier, provider, cache_envelope, user_prompt, warm_cache=True, user_confirmed=False):
        self.calls.append((tier, user_prompt[:40]))
        script = self.scripts[tier]
        text = script.pop(0) if script else "[script exhausted]"
        return text, self.COSTS[tier]

    def select_model(self, tier, provider="anthropic"):
        return f"stub-{tier}-model"


class StubEscalator:
    def __init__(self):
        self.called = False

    def package_and_escalate(self, **kwargs):
        self.called = True
        self.kwargs = kwargs
        return "[Thinker final implementation]"


class TestCollaborationSession(unittest.TestCase):
    def setUp(self):
        self.dir = "test_workspace_collab"
        if os.path.exists(self.dir):
            shutil.rmtree(self.dir)
        os.makedirs(self.dir)
        self.memory = MemoryWrapper(workspace_path=self.dir)

    def tearDown(self):
        if os.path.exists(self.dir):
            shutil.rmtree(self.dir)

    def _transcripts(self, task_id):
        import sqlite3
        conn = sqlite3.connect(self.memory.db_path)
        rows = conn.execute(
            "SELECT role, content, model, tier FROM transcripts WHERE task_id=? ORDER BY id",
            (task_id,)
        ).fetchall()
        conn.close()
        return rows

    def test_approve_ends_session_early(self):
        client = StubClient(
            thinker_script=["CONTRACT: build X. Criteria: 1. works", "APPROVE"],
            explorer_script=["NONE", "implementation v1"],
        )
        s = CollaborationSession(client, self.memory, "collab-t1", session_budget=1.0)
        res = s.run("build X")
        self.assertEqual(res["status"], "approved")
        self.assertTrue(res["approved"])
        self.assertEqual(res["rounds_used"], 1)
        self.assertEqual(res["final_output"], "implementation v1")
        # transcript carries model/tier on every model message
        rows = self._transcripts("collab-t1")
        model_rows = [r for r in rows if r[0] == "assistant"]
        self.assertTrue(all(r[2] and r[3] for r in model_rows))

    def test_loop_terminates_at_three_rounds_then_escalates(self):
        client = StubClient(
            # contract + 3 defect reviews (never approves)
            thinker_script=["CONTRACT", "1. defect A", "1. defect B", "1. defect C", "should never be used"],
            # no question + impl + 2 revisions (no revision after final review)
            explorer_script=["NONE", "impl v1", "impl v2", "impl v3", "should never be used"],
        )
        esc = StubEscalator()
        s = CollaborationSession(client, self.memory, "collab-t2", session_budget=5.0, escalator=esc)
        res = s.run("build Y")
        self.assertEqual(res["status"], "escalated")
        self.assertEqual(res["rounds_used"], 3)
        self.assertTrue(esc.called)
        # exactly 3 reviews and 2 revisions: thinker calls = 1 contract + 3 reviews
        thinker_calls = [c for c in client.calls if c[0] == "thinker"]
        explorer_calls = [c for c in client.calls if c[0] == "explorer"]
        self.assertEqual(len(thinker_calls), 4)
        self.assertEqual(len(explorer_calls), 4)  # question + impl + 2 revisions
        # escalated final output comes from the Thinker
        self.assertEqual(res["final_output"], "[Thinker final implementation]")

    def test_clarifying_question_limited_to_one(self):
        client = StubClient(
            thinker_script=["CONTRACT", "answer to the one question", "APPROVE"],
            explorer_script=["What about edge case Z?", "impl v1"],
        )
        s = CollaborationSession(client, self.memory, "collab-t3", session_budget=1.0)
        res = s.run("build Z")
        self.assertEqual(res["questions_used"], 1)
        self.assertEqual(res["status"], "approved")
        # thinker answered exactly once between contract and review
        thinker_calls = [c for c in client.calls if c[0] == "thinker"]
        self.assertEqual(len(thinker_calls), 3)  # contract, answer, review

    def test_budget_cap_halts_mid_loop(self):
        client = StubClient(
            thinker_script=["CONTRACT", "1. defect", "1. defect", "1. defect"],
            explorer_script=["NONE", "impl", "impl", "impl"],
        )
        # Budget covers contract ($0.01) + question ($0.002) + impl ($0.002)
        # + first review ($0.01) = $0.024, then halts before completing loop.
        s = CollaborationSession(client, self.memory, "collab-t4", session_budget=0.025)
        res = s.run("build W")
        self.assertEqual(res["status"], "budget_halted")
        self.assertLess(res["rounds_used"], 3)
        # halt marker present in transcript
        rows = self._transcripts("collab-t4")
        self.assertTrue(any("HALTED" in r[1] for r in rows if r[0] == "system"))

    def test_ledger_permission_error_halts(self):
        class BlockedClient(StubClient):
            def execute_task(self, *a, **k):
                raise PermissionError("Ledger block: reserve floor")
        client = BlockedClient([], [])
        s = CollaborationSession(client, self.memory, "collab-t5", session_budget=1.0)
        res = s.run("anything")
        self.assertEqual(res["status"], "ledger_halted")


if __name__ == "__main__":
    unittest.main()


class TestApiErrorHalt(unittest.TestCase):
    def setUp(self):
        self.dir = "test_workspace_collab_api"
        if os.path.exists(self.dir):
            shutil.rmtree(self.dir)
        os.makedirs(self.dir)
        self.memory = MemoryWrapper(workspace_path=self.dir)

    def tearDown(self):
        if os.path.exists(self.dir):
            shutil.rmtree(self.dir)

    def test_consecutive_api_errors_halt_session(self):
        err = "[SIMULATED FALLBACK — API ERROR, NO SPEND RECORDED: boom]\nfiller"
        client = StubClient(
            thinker_script=[err, err, err],
            explorer_script=[err, err, err],
        )
        s = CollaborationSession(client, self.memory, "collab-api1", session_budget=5.0)
        res = s.run("anything")
        self.assertEqual(res["status"], "api_error")
        # halted after 2 consecutive failures, not after burning all rounds
        self.assertLessEqual(len(client.calls), 3)
