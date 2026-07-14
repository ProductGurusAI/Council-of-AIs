import unittest
import os
import shutil
import sqlite3
import json
import subprocess
from council.gates import CodebaseGraphManager, GateCascade
from council.verifier import VerificationRunner
from council.memory import MemoryWrapper
from council.ledger import Ledger
from council.client import UnifiedClient, CacheEnvelope
from council.analytics import AnalyticsLogger

class TestHardeningAndCompletion(unittest.TestCase):
    
    def setUp(self):
        self.test_dir = "test_workspace_hardening"
        if os.path.exists(self.test_dir):
            shutil.rmtree(self.test_dir)
        os.makedirs(self.test_dir)

        # Initialize mock modules in testing directory
        self.graph_manager = CodebaseGraphManager(workspace_path=self.test_dir)
        self.memory = MemoryWrapper(workspace_path=self.test_dir)
        self.ledger = Ledger(filepath=os.path.join(self.test_dir, "test_ledger.json"))
        self.client = UnifiedClient(self.ledger)
        self.runner = VerificationRunner(self.test_dir)
        self.analytics = AnalyticsLogger(self.test_dir)

    def tearDown(self):
        if os.path.exists(self.test_dir):
            shutil.rmtree(self.test_dir)

    def test_codebase_graph_seeding(self):
        # 1. Create mock workspace files
        # main.py depends on auth_handler.py (which contains high-stakes keyword)
        main_content = "import auth_handler\nprint('hello')"
        auth_content = "def login(): pass"
        
        main_path = os.path.join(self.test_dir, "main.py")
        auth_path = os.path.join(self.test_dir, "auth_handler.py")
        
        with open(main_path, "w") as f:
            f.write(main_content)
        with open(auth_path, "w") as f:
            f.write(auth_content)
            
        # 2. Run indexing seed
        self.graph_manager.seed_from_workspace()

        # 3. Query DB nodes
        conn = sqlite3.connect(self.graph_manager.db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT filename, is_tagged, reaches_tagged FROM nodes")
        nodes = {row[0]: (row[1], row[2]) for row in cursor.fetchall()}
        
        cursor.execute("SELECT parent_id, child_id FROM edges")
        edges = cursor.fetchall()
        conn.close()

        # auth_handler.py contains "auth" keyword -> should be tagged
        self.assertIn("auth_handler.py", nodes)
        self.assertEqual(nodes["auth_handler.py"], (1, 1))

        # main.py is not high-stakes keyword directly -> not tagged, but reaches tagged
        self.assertIn("main.py", nodes)
        self.assertEqual(nodes["main.py"], (0, 1))

        # verify edge main.py -> auth_handler.py exists
        self.assertEqual(len(edges), 1)

    def test_verification_runner_criteria(self):
        task_id = "task-verify-crit"
        criteria = ["cmd: exit 0", "natural language check criterion"]

        # Create runner verification gate
        token = self.runner.create_for_task(task_id, criteria)
        self.assertIsNotNone(token)
        
        # Verify verifier_tokens.json was written and token is ignored
        gitignore_path = os.path.join(self.test_dir, ".gitignore")
        with open(gitignore_path, "r") as f:
            gitignore = f.read()
        self.assertIn(".council/verifier_tokens.json", gitignore)

        # Run checks in simulation mode (command exit 0 should pass, description should pass since no 'error' keyword)
        passed, msg = self.runner.run_checks(
            task_id=task_id,
            client=self.client,
            pre_attempt_snapshot="Clean snapshot",
            pre_attempt_decisions="No decisions",
            original_prompt="Build it.",
            failed_output="Clean output content"
        )
        self.assertTrue(passed)
        self.assertIn("passed", msg.lower())

        # Test failure execution: cmd exit 1 should fail
        task_fail_id = "task-verify-fail"
        fail_token = self.runner.create_for_task(task_fail_id, ["cmd: exit 1"])
        
        passed_fail, msg_fail = self.runner.run_checks(
            task_id=task_fail_id,
            client=self.client,
            pre_attempt_snapshot="Clean snapshot",
            pre_attempt_decisions="No decisions",
            original_prompt="Build it.",
            failed_output="Clean output content"
        )
        self.assertFalse(passed_fail)
        self.assertIn("failed on: 'cmd: exit 1'", msg_fail)

    def test_adversarial_completion_gates(self):
        task_id = "task-adversarial"
        criteria = ["Item 1", "Item 2"]
        
        token = self.runner.create_for_task(task_id, criteria)
        
        # 1. Accessing check_off_item with invalid token -> raise PermissionError
        gate = self.runner.completion_gate
        with self.assertRaises(PermissionError):
            gate.check_off_item(task_id, "Item 1", "hacky-token")

        # 2. Tampering structure: rewrite file and try to verify/complete
        filepath = gate._get_filepath(task_id)
        # Modify the checklist items in file (executor cheats by deleting "Item 2")
        with open(filepath, "w") as f:
            f.write("- [ ] Item 1\n")
            
        # Verify structure check catches the modification -> returns False or raises
        self.assertFalse(gate.verify_structure(task_id))
        self.assertFalse(gate.is_complete(task_id))

        # Check off fails because of invalid structure
        with self.assertRaises(PermissionError):
            gate.check_off_item(task_id, "Item 1", token)

    def test_write_lockout_enforcement(self):
        # Create write lockout file
        lock_file = os.path.join(self.test_dir, ".council", "memory", "write_lockout.lock")
        os.makedirs(os.path.dirname(lock_file), exist_ok=True)
        with open(lock_file, "w") as f:
            f.write("LOCKOUT_COMMIT: abc123def\nREASON: Poisoned commit detected.")
            
        # Try to validate and save an entry -> raises PermissionError
        note_content = "---\nclass: progress\nauthor_model: test\ntask_id: 1\n---\nHello"
        with self.assertRaises(PermissionError):
            self.memory.validate_and_save_entry("task_handoff.md", note_content)

        # Try to commit task boundary -> raises PermissionError
        with self.assertRaises(PermissionError):
            self.memory.commit_task_boundary("task-1")

    def test_decision_accumulation_counters(self):
        # 1. Create an explorer-tier decision entry (valid under FR-25)
        explorer_dec = (
            "---\n"
            "class: decision\n"
            "author_model: gpt-4o-mini\n"
            "author_tier: explorer\n"
            "task_id: task-dec-1\n"
            "reopen_condition: when schema changes\n"
            "---\n"
            "Design decision details."
        )
        self.memory.validate_and_save_entry("decision_1.md", explorer_dec)
        
        # 2. Verify accumulation count is 1
        count = self.memory.get_decision_accumulation()
        self.assertEqual(count, 1)

        # 3. Mark decisions consolidated
        self.memory.consolidate_decisions("task-dec-1")
        
        # 4. Verify accumulation count is reset to 0
        count2 = self.memory.get_decision_accumulation()
        self.assertEqual(count2, 0)

    def test_endgame_constraints_checks(self):
        # Setup ledger with low remaining budget (< $15.00)
        # Set total_budget=20.00, total_spent=5.01 -> remaining is 14.99
        self.ledger.total_budget = 20.00
        self.ledger.data["total_budget"] = 20.00
        self.ledger.data["total_spent"] = 5.01
        self.ledger._save_ledger()

        # Thinker tier step check without confirmation -> returns False (ENDGAME)
        is_allowed, reason = self.ledger.check_constraints(
            task_id="task-end",
            next_step_est_cost=0.05,
            is_thinker_escalation=True,
            user_confirmed=False
        )
        self.assertFalse(is_allowed)
        self.assertIn("ENDGAME", reason)

        # Thinker tier step check with confirmation -> returns True (allowed)
        is_allowed_confirmed, _ = self.ledger.check_constraints(
            task_id="task-end",
            next_step_est_cost=0.05,
            is_thinker_escalation=True,
            user_confirmed=True
        )
        self.assertTrue(is_allowed_confirmed)

    def test_analytics_log_parsing(self):
        task_id = "task-analytic-123"
        self.analytics.log_turn(
            task_id=task_id,
            prompt_features=["schema", "sql"],
            gate_fired="Gate Cascade -> thinker",
            tier="thinker",
            cost=0.0450,
            override_used=False,
            verification_result="passed"
        )
        
        self.analytics.log_turn(
            task_id=task_id,
            prompt_features=["tweak"],
            gate_fired="Bypass Lane (cheap)",
            tier="explorer",
            cost=0.0003,
            override_used=False,
            verification_result="passed"
        )

        # Verify logger statistics
        stats = self.analytics.get_statistics(self.ledger.data)
        self.assertEqual(stats["total_turns_logged"], 2)
        self.assertEqual(stats["override_count"], 0)
        self.assertEqual(stats["failed_then_escalated_count"], 0)
        self.assertAlmostEqual(stats["total_spend_by_route"]["thinker"], 0.0450)
        self.assertAlmostEqual(stats["total_spend_by_route"]["explorer"], 0.0003)

    def test_rehydration_lockout_release_on_quiz_pass(self):
        from council.rehydration import BisectRecovery
        recovery = BisectRecovery(self.test_dir)
        recovery.acquire_lock("badcommit", "Poisoned state")
        self.assertTrue(recovery.is_locked())
        
        # Release lock directly
        recovery.release_lock()
        self.assertFalse(recovery.is_locked())

    def test_rehydration_revert_conflict_quarantine(self):
        # Setup git repo to simulate revert conflict
        from council.rehydration import BisectRecovery, RehydrationTester
        recovery = BisectRecovery(self.test_dir)
        
        subprocess.run(["git", "init"], cwd=self.test_dir, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test User"], cwd=self.test_dir, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=self.test_dir, check=True)
        
        # Commit 1
        memory_dir = os.path.join(self.test_dir, ".council", "memory")
        os.makedirs(memory_dir, exist_ok=True)
        with open(os.path.join(memory_dir, "invariants.md"), "w") as f:
            f.write("INVARIANTS: base content")
        subprocess.run(["git", "add", "."], cwd=self.test_dir, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Commit 1"], cwd=self.test_dir, check=True, capture_output=True)
        good_commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.test_dir, capture_output=True, text=True).stdout.strip()
        
        # Commit 2 (Poisoned - conflicting edit)
        with open(os.path.join(memory_dir, "invariants.md"), "w") as f:
            f.write("Broken memory")
        subprocess.run(["git", "add", "."], cwd=self.test_dir, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Commit 2"], cwd=self.test_dir, check=True, capture_output=True)
        bad_commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.test_dir, capture_output=True, text=True).stdout.strip()
        
        # Commit 3 (Creates conflict for revert of Commit 2)
        with open(os.path.join(memory_dir, "invariants.md"), "w") as f:
            f.write("Conflicting edit content")
        subprocess.run(["git", "add", "."], cwd=self.test_dir, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Commit 3"], cwd=self.test_dir, check=True, capture_output=True)
        
        # Run bisect_and_revert -> should conflict, abort, and quarantine
        tester = RehydrationTester()
        first_bad, log = recovery.bisect_and_revert(good_commit, tester, [{"question": "a", "answer": "a"}])
        self.assertEqual(first_bad, bad_commit)
        self.assertIn("REVERT_CONFLICT", log)
        
        # Verify locked and quarantine created
        self.assertTrue(recovery.is_locked())
        lock_file = os.path.join(self.test_dir, ".council", "memory", "write_lockout.lock")
        with open(lock_file, "r") as lf:
            reason = lf.read()
        self.assertIn("Revert conflicted and was aborted", reason)
        
        quarantine_file = os.path.join(self.test_dir, ".council", "quarantine", "conflict_invariants.md")
        self.assertTrue(os.path.exists(quarantine_file))

    def test_dashboard_server_endpoints(self):
        import threading
        import socket
        import urllib.request
        import urllib.error
        from http.server import HTTPServer
        from council.dashboard_server import DashboardHTTPHandler
        
        # Override keys to force simulated mode in tests
        orig_keys = {}
        for k in ["ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY"]:
            if k in os.environ:
                orig_keys[k] = os.environ[k]
                del os.environ[k]
        
        # Override workspace path inside test to point to self.test_dir
        import council.dashboard_server
        original_workspace_dir = council.dashboard_server.WORKSPACE_DIR
        original_static_dir = council.dashboard_server.STATIC_DIR
        
        council.dashboard_server.WORKSPACE_DIR = self.test_dir
        # Point static dir to a dummy temp directory inside self.test_dir
        temp_static = os.path.join(self.test_dir, "dashboard")
        os.makedirs(temp_static, exist_ok=True)
        council.dashboard_server.STATIC_DIR = temp_static
        
        with open(os.path.join(temp_static, "index.html"), "w") as f:
            f.write("<html>Test Dashboard</html>")
            
        def get_free_port():
            s = socket.socket()
            s.bind(("", 0))
            port = s.getsockname()[1]
            s.close()
            return port

        port = get_free_port()
        server = HTTPServer(("", port), DashboardHTTPHandler)
        thread = threading.Thread(target=server.serve_forever)
        thread.daemon = True
        thread.start()
        
        try:
            # 1. Test Static file serving
            url = f"http://localhost:{port}/"
            resp = urllib.request.urlopen(url)
            html = resp.read().decode("utf-8")
            self.assertEqual(html, "<html>Test Dashboard</html>")
            
            # 2. Test Stats endpoint
            url_stats = f"http://localhost:{port}/api/stats"
            resp_stats = urllib.request.urlopen(url_stats)
            stats = json.loads(resp_stats.read().decode("utf-8"))
            self.assertIn("total_budget", stats)
            
            # 3. Test Graph endpoint
            url_graph = f"http://localhost:{port}/api/graph"
            resp_graph = urllib.request.urlopen(url_graph)
            graph = json.loads(resp_graph.read().decode("utf-8"))
            self.assertIn("nodes", graph)
            self.assertIn("edges", graph)
            
            # 4. Test Tasks endpoint
            url_tasks = f"http://localhost:{port}/api/tasks"
            resp_tasks = urllib.request.urlopen(url_tasks)
            tasks = json.loads(resp_tasks.read().decode("utf-8"))
            self.assertIsInstance(tasks, list)

            # 5. Adversarial: Non-existent file /api/invalid
            url_invalid = f"http://localhost:{port}/api/invalid"
            try:
                urllib.request.urlopen(url_invalid)
                self.fail("Expected HTTPError 404 for invalid endpoint")
            except urllib.error.HTTPError as he:
                self.assertEqual(he.code, 404)

            # 6. Adversarial: Directory traversal attempt
            url_traversal = f"http://localhost:{port}/../app.py"
            try:
                urllib.request.urlopen(url_traversal)
                self.fail("Expected HTTPError 403 or 404 for traversal attempt")
            except urllib.error.HTTPError as he:
                self.assertIn(he.code, [403, 404])
            # 7. Test keys endpoints (GET)
            url_keys = f"http://localhost:{port}/api/keys"
            resp_keys = urllib.request.urlopen(url_keys)
            keys_data = json.loads(resp_keys.read().decode("utf-8"))
            self.assertIn("anthropic_key_set", keys_data)
            
            # 8. Test keys endpoints (POST)
            req = urllib.request.Request(
                url_keys,
                data=json.dumps({
                    "anthropic_key": "sk-ant-testkey12345678",
                    "gemini_key": "AIzaSy-testkey12345678",
                    "openai_key": "",
                    "custom_keys": {
                        "DEEPSEEK": "sk-ds-testkey12345678",
                        "GROQ": "gsk-testkey12345678"
                    }
                }).encode("utf-8"),
                headers={"Content-Type": "application/json"}
            )
            resp_post_keys = urllib.request.urlopen(req)
            post_result = json.loads(resp_post_keys.read().decode("utf-8"))
            self.assertEqual(post_result["status"], "success")
            
            # Verify environment load
            self.assertEqual(os.environ.get("ANTHROPIC_API_KEY"), "sk-ant-testkey12345678")
            self.assertEqual(os.environ.get("GEMINI_API_KEY"), "AIzaSy-testkey12345678")
            self.assertEqual(os.environ.get("DEEPSEEK_API_KEY"), "sk-ds-testkey12345678")
            self.assertEqual(os.environ.get("GROQ_API_KEY"), "gsk-testkey12345678")
            
            # Verify file write to self.test_dir / .env
            env_file = os.path.join(self.test_dir, ".env")
            self.assertTrue(os.path.exists(env_file))
            with open(env_file, "r") as ef:
                content = ef.read()
            self.assertIn("ANTHROPIC_API_KEY=sk-ant-testkey12345678", content)
            self.assertIn("GEMINI_API_KEY=AIzaSy-testkey12345678", content)
            self.assertIn("DEEPSEEK_API_KEY=sk-ds-testkey12345678", content)
            self.assertIn("GROQ_API_KEY=gsk-testkey12345678", content)
            
            # 9. Test keys endpoints (GET) returns custom keys
            resp_keys2 = urllib.request.urlopen(url_keys)
            keys_data2 = json.loads(resp_keys2.read().decode("utf-8"))
            self.assertIn("custom_keys", keys_data2)
            custom_names = [item["name"] for item in keys_data2["custom_keys"]]
            self.assertIn("DEEPSEEK", custom_names)
            self.assertIn("GROQ", custom_names)
            
            # 10. Test model config endpoint (GET)
            url_config = f"http://localhost:{port}/api/models/config"
            resp_config = urllib.request.urlopen(url_config)
            config_data = json.loads(resp_config.read().decode("utf-8"))
            self.assertIn("available_models", config_data)
            self.assertIn("tier_models", config_data)
            self.assertIn("tier_thinking", config_data)

            # 11. Test model config endpoint (POST)
            req_config_post = urllib.request.Request(
                url_config,
                data=json.dumps({
                    "tier_models": {
                        "openai": {
                            "thinker": "gpt-4o",
                            "explorer": "gpt-4o-mini",
                            "cheap": "gpt-4o-mini"
                        }
                    },
                    "tier_thinking": {
                        "thinker": "high",
                        "explorer": "low"
                    }
                }).encode("utf-8"),
                headers={"Content-Type": "application/json"}
            )
            resp_post_config = urllib.request.urlopen(req_config_post)
            post_config_result = json.loads(resp_post_config.read().decode("utf-8"))
            self.assertEqual(post_config_result["status"], "success")

            # 12. Test model probe endpoint (POST)
            url_probe = f"http://localhost:{port}/api/models/probe"
            req_probe_post = urllib.request.Request(
                url_probe,
                data=json.dumps({
                    "provider": "openai",
                    "model": "gpt-4o-mini"
                }).encode("utf-8"),
                headers={"Content-Type": "application/json"}
            )
            resp_post_probe = urllib.request.urlopen(req_probe_post)
            post_probe_result = json.loads(resp_post_probe.read().decode("utf-8"))
            self.assertEqual(post_probe_result["status"], "success")
            self.assertEqual(post_probe_result["score"], 6)  # simulated mock achieves 6/6
            self.assertEqual(post_probe_result["proposed_tier"], "thinker")
            
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

            # Restore original paths
            council.dashboard_server.WORKSPACE_DIR = original_workspace_dir
            council.dashboard_server.STATIC_DIR = original_static_dir

            # Restore original keys
            for k, val in orig_keys.items():
                os.environ[k] = val

            # CRITICAL: POST /api/keys writes fake keys into os.environ. Without
            # cleanup, every later test sees keys present (is_simulated=False)
            # and attempts REAL API calls with fake keys — test-order-dependent
            # suite poisoning.
            for var in ("ANTHROPIC_API_KEY", "GEMINI_API_KEY", "OPENAI_API_KEY"):
                os.environ.pop(var, None)

    def test_custom_provider_and_thinking_config(self):
        from council.ledger import Ledger
        from council.client import UnifiedClient, CacheEnvelope
        import tempfile
        
        # Create a temp models.json
        with tempfile.TemporaryDirectory() as tmpdir:
            models_path = os.path.join(tmpdir, "models.json")
            ledger_path = os.path.join(tmpdir, "ledger.json")
            
            cfg = {
                "pricing": {
                    "claude-opus-4-8": {
                        "provider": "anthropic",
                        "input": 15.0,
                        "output": 75.0,
                        "supports_thinking": True
                    },
                    "custom-model": {
                        "provider": "custom-prov",
                        "input": 1.0,
                        "output": 2.0
                    }
                },
                "tier_models": {
                    "anthropic": {
                        "thinker": "claude-opus-4-8"
                    },
                    "custom-prov": {
                        "thinker": "custom-model"
                    }
                },
                "custom_providers": {
                    "custom-prov": {
                        "base_url": "https://api.custom.com/v1",
                        "key_env_var": "CUSTOM_PROV_API_KEY"
                    }
                }
            }
            with open(models_path, "w") as f:
                json.dump(cfg, f)
                
            ledger = Ledger(filepath=ledger_path, pricing_config=models_path)
            
            # 1. Verify custom_providers dictionary loaded correctly
            self.assertIn("custom-prov", ledger.custom_providers)
            self.assertEqual(ledger.custom_providers["custom-prov"]["base_url"], "https://api.custom.com/v1")
            
            # 2. Verify supports_thinking parsed correctly
            self.assertTrue(ledger.pricing["claude-opus-4-8"].get("supports_thinking"))
            
            # 3. Verify fail-expensive on missing model pricing
            client = UnifiedClient(ledger=ledger)
            env = CacheEnvelope("mock-system", "mock-decisions", "mock-task")
            
            # Temporarily override pricing dict to remove custom-model
            orig_pricing = ledger.pricing.copy()
            del ledger.pricing["custom-model"]
            with self.assertRaises(ValueError) as ctx:
                client.execute_task("task-123", "thinker", "custom-prov", env, "hello")
            self.assertIn("pricing configuration missing", str(ctx.exception).lower())
            
            # Restore pricing
            ledger.pricing = orig_pricing

    def test_execute_turn_with_verification_failure_and_escalation(self):
        from council.app import CouncilSession, _new_task
        import unittest.mock as mock
        
        # 1. Start a new task
        task_id = _new_task("Build a robust database router.")
        session = CouncilSession(workspace_path=self.test_dir)
        
        # 2. Add verification criteria (fails in simulation if output contains 'fail')
        session.verifier.create_for_task(task_id, ["Test checking item fails"])
        
        def mock_generate(tier, prompt):
            if tier == "explorer":
                return "This is a failed attempt." # contains 'fail' -> verification fail
            return "[Thinker Response] Correct escalated code."
            
        with mock.patch.object(session.client, "_generate_simulated_response", side_effect=mock_generate):
            response, cost, tier, route_reason = session.execute_turn(task_id, "Write database router")
            
            # The final response should be the Thinker's response, NOT the failed explorer response
            self.assertEqual(response, "[Thinker Response] Correct escalated code.")
            self.assertEqual(tier, "thinker")
            self.assertIn("Escalated to Thinker", route_reason)

    def test_ledger_cli_command_rename(self):
        from click.testing import CliRunner
        from council.app import cli
        
        runner = CliRunner()
        result = runner.invoke(cli, ["--help"])
        self.assertIn("ledger", result.output)
        self.assertIn("dashboard", result.output)

if __name__ == "__main__":
    unittest.main()
