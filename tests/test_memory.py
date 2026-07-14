import unittest
import os
import shutil
import subprocess
import sqlite3
from council.memory import MemoryWrapper, CHEAP_MODELS
from council.compactor import ContextCompactor
from council.client import CacheEnvelope, UnifiedClient
from council.ledger import Ledger
from council.rehydration import RehydrationTester, BisectRecovery

class TestMemoryIntegration(unittest.TestCase):
    
    def setUp(self):
        self.test_dir = "test_workspace"
        if os.path.exists(self.test_dir):
            shutil.rmtree(self.test_dir)
        os.makedirs(self.test_dir)

        # Initialize wrapper in temp workspace
        self.wrapper = MemoryWrapper(workspace_path=self.test_dir)
        self.ledger = Ledger(filepath=os.path.join(self.test_dir, "test_ledger.json"))

    def tearDown(self):
        if os.path.exists(self.test_dir):
            shutil.rmtree(self.test_dir)

    def test_yaml_front_matter_parsing(self):
        content = (
            "---\n"
            "class: decision\n"
            "author_model: claude-3-opus-20240229\n"
            "task_id: task-12345\n"
            "---\n"
            "This is the decision rationale."
        )
        metadata, body = self.wrapper.parse_markdown_with_yaml(content)
        self.assertEqual(metadata.get("class"), "decision")
        self.assertEqual(metadata.get("author_model"), "claude-3-opus-20240229")
        self.assertEqual(metadata.get("task_id"), "task-12345")
        self.assertEqual(body.strip(), "This is the decision rationale.")

    def test_fr25_authorship_rules(self):
        # Premium model writing decision -> Allowed
        valid_content = (
            "---\n"
            "class: decision\n"
            "author_model: claude-3-opus-20240229\n"
            "task_id: task-12345\n"
            "reopen_condition: if provider pricing model changes\n"
            "---\n"
            "Decision details."
        )
        self.assertTrue(self.wrapper.validate_and_save_entry("decisions.md", valid_content))

        # Cheap model writing decision -> Banned (FR-25)
        for cheap_model in CHEAP_MODELS:
            invalid_content = (
                "---\n"
                "class: decision\n"
                f"author_model: {cheap_model}\n"
                "task_id: task-12345\n"
                "reopen_condition: never\n"
                "---\n"
                "Trivial reasoning attempt."
            )
            with self.assertRaises(PermissionError):
                self.wrapper.validate_and_save_entry("decisions.md", invalid_content)

    def test_yaml_quarantine(self):
        # Invalid YAML (missing class)
        invalid_yaml = (
            "---\n"
            "author_model: claude-3-opus-20240229\n"
            "task_id: task-12345\n"
            "---\n"
            "Body content."
        )
        with self.assertRaises(ValueError):
            self.wrapper.validate_and_save_entry("invariants.md", invalid_yaml)
        
        # Verify it was quarantined
        quarantine_file = os.path.join(self.test_dir, ".council", "quarantine", "invariants.md")
        self.assertTrue(os.path.exists(quarantine_file))

    def test_sqlite_transcripts_and_vector_similarity(self):
        # Append transcripts with mock vector embeddings
        self.wrapper.append_transcript("task-1", 1, "user", "How do I build a REST API?", embedding=[0.1, 0.2, 0.3])
        self.wrapper.append_transcript("task-1", 2, "assistant", "Use FastAPI and Uvicorn.", embedding=[0.9, 0.8, 0.7])
        
        # Test query similarity with query vector [0.1, 0.2, 0.29] (very close to user question)
        results = self.wrapper.query_similar_transcripts([0.1, 0.2, 0.29], limit=1)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0][1], "task-1")
        self.assertIn("REST API", results[0][2])
        self.assertGreater(results[0][3], 0.95) # High cosine similarity score

    def test_context_compactor(self):
        compactor = ContextCompactor(max_capacity_tokens=1000)
        
        # Setup CacheEnvelope
        env = CacheEnvelope("System context", "Decisions", "Canvas")
        env.add_turn("user", "Initial question")
        env.add_turn("assistant", "Step 1 details")
        env.add_turn("user", "Step 2 details")
        env.add_turn("assistant", "Step 3 details")
        
        # 1. Verify mild compaction (drops middle turns, keeps first + last 2,
        # merges the strip marker into a retained turn — NO system-role turns,
        # and consecutive same-role turns are merged to keep alternation valid)
        compactor.compact_mild(env)
        roles = [t["role"] for t in env.layer4_turns]
        self.assertNotIn("system", roles)
        for a, b in zip(roles, roles[1:]):
            self.assertNotEqual(a, b)  # strict user/assistant alternation
        self.assertIn("Initial question", env.layer4_turns[0]["content"])
        joined = "\n".join(t["content"] for t in env.layer4_turns)
        self.assertIn("Stale intermediate context stripped", joined)
        self.assertEqual(env.layer4_turns[-1]["content"], "Step 3 details")

        # 2. Verify aggressive compaction (strips verbose JSON logs)
        env_aggr = CacheEnvelope("System", "Decisions", "Canvas")
        verbose_content = "Here is the raw logs:\n```json\n{\n\"tool_output\": \"very verbose details\"\n}\n```\nDone."
        env_aggr.add_turn("user", "run tools")
        env_aggr.add_turn("assistant", verbose_content)
        
        # Assert verbose string contains json tool_output block
        self.assertIn("tool_output", env_aggr.layer4_turns[1]["content"])
        
        # Run aggressive compaction (rebuild is cheap, savings are large)
        # Mock cost calculator returning $0.01 for rebuild and $0.05 for savings
        def mock_calc(model, input_tokens=0, output_tokens=0, cache_write_tokens=0, cache_read_tokens=0):
            if cache_write_tokens > 0:
                return 0.005 # cost to rebuild prefix
            return 0.02 # savings per turn
            
        success, reason = compactor.compact_aggressive(env_aggr, mock_calc, remaining_turns_est=5)
        self.assertTrue(success)
        self.assertNotIn("tool_output", env_aggr.layer4_turns[1]["content"])
        self.assertIn("Raw logs stripped", env_aggr.layer4_turns[1]["content"])

    def test_rehydration_bisect_and_revert(self):
        # We need a valid git repository inside test_workspace to run real git tests
        recovery = BisectRecovery(self.test_dir)
        
        # Initialize initial git repo with memory files
        subprocess.run(["git", "config", "user.name", "Test User"], cwd=self.test_dir, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=self.test_dir, check=True)
        
        memory_dir = os.path.join(self.test_dir, ".council", "memory")
        
        # Commit 1: Good base
        base_content = "---\nclass: invariant\nauthor_model: claude-3-opus-20240229\ntask_id: task-base\n---\nSystem INVARIANTS"
        with open(os.path.join(memory_dir, "invariants.md"), "w") as f:
            f.write(base_content)
        subprocess.run(["git", "add", "."], cwd=self.test_dir, check=True)
        subprocess.run(["git", "commit", "-m", "Commit 1 (Good Base)"], cwd=self.test_dir, check=True)
        good_commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.test_dir, capture_output=True, text=True).stdout.strip()

        # Commit 2: Poisoned entry (removes Invariants completely)
        poisoned_content = "---\nclass: invariant\nauthor_model: claude-3-opus-20240229\ntask_id: task-poison\n---\nBroken memory"
        with open(os.path.join(memory_dir, "invariants.md"), "w") as f:
            f.write(poisoned_content)
        subprocess.run(["git", "add", "."], cwd=self.test_dir, check=True)
        subprocess.run(["git", "commit", "-m", "Commit 2 (Poisoned)"], cwd=self.test_dir, check=True)
        bad_commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.test_dir, capture_output=True, text=True).stdout.strip()
        
        # Setup Quiz Tester (Simulated)
        quiz_questions = [{"question": "Is system running?", "answer": "Yes"}]
        quizzer = RehydrationTester()

        # Score on HEAD (bad_commit) should fail (no 'INVARIANTS' text in invariants.md)
        score_bad = quizzer.score_quiz(poisoned_content, quiz_questions)
        self.assertLess(score_bad, 0.8)

        # Score on base_commit should pass (contains 'INVARIANTS')
        score_good = quizzer.score_quiz(base_content, quiz_questions)
        self.assertGreaterEqual(score_good, 0.8)

        # Run Bisect and Revert
        first_bad, log = recovery.bisect_and_revert(good_commit, quizzer, quiz_questions)
        
        # Verify bisect located the bad commit
        self.assertEqual(first_bad, bad_commit)
        
        # Verify the write lockout lock was generated
        self.assertTrue(recovery.is_locked())
        
        # Verify file reverted back to Good Base (git revert restored base_content)
        with open(os.path.join(memory_dir, "invariants.md"), "r") as f:
            restored = f.read()
        self.assertIn("INVARIANTS", restored)

if __name__ == "__main__":
    unittest.main()
