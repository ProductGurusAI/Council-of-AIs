import os
import json
import re
import sqlite3
import subprocess
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs
from typing import Dict, Any, List

# Setup directories
COUNCIL_DIR = os.path.dirname(os.path.abspath(__file__))
WORKSPACE_DIR = os.path.dirname(COUNCIL_DIR)

# Collaboration Sessions run in background threads; results land here keyed by
# task_id. The Chamber UI polls /api/task for the live transcript and
# /api/collab/result for the final summary.
COLLAB_RESULTS: Dict[str, Any] = {}
STATIC_DIR = os.path.join(COUNCIL_DIR, "dashboard")

class DashboardHTTPHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # Silence default terminal logs to keep Click output clean
        pass

    def _set_headers(self, content_type: str = "application/json", status: int = 200):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        # No caching: the dashboard is local and evolves fast — stale cached JS
        # against a newer server produces silent breakage (e.g. unpopulated
        # mapping dropdowns).
        self.send_header("Cache-Control", "no-store, must-revalidate")
        self.end_headers()

    def do_OPTIONS(self):
        self._set_headers(status=204)

    def do_GET(self):
        parsed_url = urlparse(self.path)
        path = parsed_url.path.rstrip("/")
        query = parse_qs(parsed_url.query)

        # ----------------------------------------------------
        # API Endpoints
        # ----------------------------------------------------
        if path == "/api/stats":
            self.handle_get_stats()
        elif path == "/api/tasks":
            self.handle_get_tasks()
        elif path == "/api/task":
            task_id = query.get("id", [None])[0]
            self.handle_get_task(task_id)
        elif path == "/api/graph":
            self.handle_get_graph()
        elif path == "/api/keys":
            self.handle_get_keys()
        elif path == "/api/models/config":
            self.handle_get_models_config()
        elif path == "/api/collab/result":
            self.handle_get_collab_result(query.get("id", [None])[0])
        # ----------------------------------------------------
        # Static Files Serving
        # ----------------------------------------------------
        else:
            self.handle_static_file(parsed_url.path)

    def do_POST(self):
        parsed_url = urlparse(self.path)
        path = parsed_url.path.rstrip("/")

        # Read JSON body
        content_length = int(self.headers.get("Content-Length", 0))
        post_data = self.rfile.read(content_length) if content_length > 0 else b""
        body = {}
        if post_data:
            try:
                body = json.loads(post_data.decode("utf-8"))
            except Exception:
                pass

        if path == "/api/playground/run":
            self.handle_post_playground_run(body)
        elif path == "/api/playground/close":
            self.handle_post_playground_close(body)
        elif path == "/api/keys":
            self.handle_post_keys(body)
        elif path == "/api/models/config":
            self.handle_post_models_config(body)
        elif path == "/api/models/probe":
            self.handle_post_models_probe(body)
        elif path == "/api/models/bench":
            self.handle_post_models_bench(body)
        elif path == "/api/collab/run":
            self.handle_post_collab_run(body)
        else:
            self._set_headers(status=404)
            self.wfile.write(json.dumps({"error": "Endpoint not found"}).encode())

    # ========================================================
    # GET Handlers
    # ========================================================
    def handle_get_stats(self):
        try:
            from council.ledger import Ledger
            from council.analytics import AnalyticsLogger

            ledger = Ledger(filepath=os.path.join(WORKSPACE_DIR, "ledger_store.json"), pricing_config=os.path.join(WORKSPACE_DIR, "models.json"))
            logger = AnalyticsLogger(workspace_path=WORKSPACE_DIR)
            stats = logger.get_statistics(ledger.data)

            response = {
                "total_budget": ledger.data.get("total_budget", 75.00),
                "remaining_budget": ledger.get_remaining_budget(),
                "total_spent": ledger.data.get("total_spent", 0.0),
                "reserve_floor": ledger.reserve_floor,
                "per_task_cap": ledger.per_task_cap,
                "total_turns_logged": stats.get("total_turns_logged", 0),
                "override_count": stats.get("override_count", 0),
                "failed_then_escalated_count": stats.get("failed_then_escalated_count", 0),
                "orchestration_spent": stats.get("orchestration_spent", 0.0),
                "orchestration_overhead_pct": stats.get("orchestration_overhead_pct", 0.0),
                "avg_spend_per_turn_by_route": stats.get("avg_spend_per_turn_by_route", {})
            }
            self._set_headers()
            self.wfile.write(json.dumps(response).encode())
        except Exception as e:
            self._set_headers(status=500)
            self.wfile.write(json.dumps({"error": str(e)}).encode())

    def handle_get_tasks(self):
        try:
            from council.ledger import Ledger
            ledger = Ledger(filepath=os.path.join(WORKSPACE_DIR, "ledger_store.json"), pricing_config=os.path.join(WORKSPACE_DIR, "models.json"))
            
            tasks_dir = os.path.join(WORKSPACE_DIR, "tasks_store")
            tasks = []
            if os.path.exists(tasks_dir):
                for filename in os.listdir(tasks_dir):
                    if filename.endswith(".json") and not filename.endswith("_gate.json") and not filename.endswith(".gate"):
                        task_id = filename.replace(".json", "")
                        filepath = os.path.join(tasks_dir, filename)
                        try:
                            with open(filepath, "r") as f:
                                data = json.load(f)
                            
                            # Extract goal from layer3
                            layer3 = data.get("layer3", "")
                            goal = "Unknown Task Goal"
                            m = re.search(r"Goal:\s*(.*)", layer3)
                            if m:
                                goal = m.group(1).strip()
                            
                            # Get task spend from ledger
                            task_spent = ledger.get_task_spend(task_id)
                            tasks.append({
                                "task_id": task_id,
                                "goal": goal,
                                "total_spent": task_spent
                            })
                        except Exception:
                            pass
            
            # Sort by task_id descending
            tasks.sort(key=lambda t: t["task_id"], reverse=True)

            self._set_headers()
            self.wfile.write(json.dumps(tasks).encode())
        except Exception as e:
            self._set_headers(status=500)
            self.wfile.write(json.dumps({"error": str(e)}).encode())

    def handle_get_task(self, task_id: str):
        if not task_id:
            self._set_headers(status=400)
            self.wfile.write(json.dumps({"error": "task_id query parameter is required"}).encode())
            return

        try:
            from council.memory import MemoryWrapper
            from council.ledger import Ledger
            
            tasks_dir = os.path.join(WORKSPACE_DIR, "tasks_store")
            filepath = os.path.join(tasks_dir, f"{task_id}.json")
            # Collaboration sessions are transcript-only: they have no envelope
            # file in tasks_store, so a missing file is not automatically a 404.
            data = {}
            if os.path.exists(filepath):
                with open(filepath, "r") as f:
                    data = json.load(f)
            elif not task_id.startswith("collab-"):
                self._set_headers(status=404)
                self.wfile.write(json.dumps({"error": f"Task {task_id} not found"}).encode())
                return

            layer3 = data.get("layer3", "")
            goal = "Collaboration Session" if task_id.startswith("collab-") else "Unknown Goal"
            m = re.search(r"Goal:\s*(.*)", layer3)
            if m:
                goal = m.group(1).strip()

            # Load checklist if exists
            checklist = []
            gates_dir = os.path.join(WORKSPACE_DIR, ".council", "completion_gates")
            gate_path = os.path.join(gates_dir, f"{task_id}.gate")
            if os.path.exists(gate_path):
                try:
                    with open(gate_path, "r") as f:
                        for line in f:
                            line_str = line.strip()
                            if line_str.startswith("- [ ]") or line_str.startswith("- [x]"):
                                status = "completed" if "[x]" in line_str else "pending"
                                criterion = line_str[5:].strip()
                                checklist.append({"criterion": criterion, "status": status})
                except Exception:
                    pass

            # Load transcripts from SQLite
            transcripts = []
            memory = MemoryWrapper(workspace_path=WORKSPACE_DIR)
            try:
                conn = sqlite3.connect(memory.db_path)
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT turn_index, role, content, cost, model, tier FROM transcripts WHERE task_id = ? ORDER BY id ASC",
                    (task_id,)
                )
                rows = cursor.fetchall()
                conn.close()
                for r in rows:
                    transcripts.append({
                        "turn_index": r[0],
                        "role": r[1],
                        "content": r[2],
                        "model": r[4] if len(r) > 4 else None,
                        "tier": r[5] if len(r) > 5 else None,
                        "cost": r[3]
                    })
            except Exception:
                pass

            ledger = Ledger(filepath=os.path.join(WORKSPACE_DIR, "ledger_store.json"), pricing_config=os.path.join(WORKSPACE_DIR, "models.json"))
            response = {
                "task_id": task_id,
                "goal": goal,
                "total_spent": ledger.get_task_spend(task_id),
                "envelope": {
                    "layer1": data.get("layer1", ""),
                    "layer2": data.get("layer2", ""),
                    "layer3": data.get("layer3", "")
                },
                "checklist": checklist,
                "transcripts": transcripts
            }

            self._set_headers()
            self.wfile.write(json.dumps(response).encode())
        except Exception as e:
            self._set_headers(status=500)
            self.wfile.write(json.dumps({"error": str(e)}).encode())

    def handle_get_graph(self):
        try:
            from council.gates import CodebaseGraphManager
            graph = CodebaseGraphManager(workspace_path=WORKSPACE_DIR)
            
            conn = sqlite3.connect(graph.db_path)
            cursor = conn.cursor()
            
            # Nodes
            cursor.execute("SELECT filename, is_tagged, reaches_tagged FROM nodes")
            nodes = [{"filename": r[0], "is_tagged": bool(r[1]), "reaches_tagged": bool(r[2])} for r in cursor.fetchall()]
            
            # Edges
            cursor.execute("""
                SELECT n1.filename, n2.filename, e.dependency_type 
                FROM edges e 
                JOIN nodes n1 ON e.parent_id = n1.id 
                JOIN nodes n2 ON e.child_id = n2.id
            """)
            edges = [{"parent": r[0], "child": r[1], "type": r[2]} for r in cursor.fetchall()]
            conn.close()

            response = {
                "nodes": nodes,
                "edges": edges
            }
            self._set_headers()
            self.wfile.write(json.dumps(response).encode())
        except Exception as e:
            self._set_headers(status=500)
            self.wfile.write(json.dumps({"error": str(e)}).encode())

    # ========================================================
    # POST Handlers
    # ========================================================
    def handle_post_collab_run(self, body: Dict[str, Any]):
        goal = (body.get("goal") or "").strip()
        try:
            budget = float(body.get("budget", 0.50))
        except (TypeError, ValueError):
            budget = 0.50
        budget = max(0.01, min(budget, 5.00))  # sane per-session bounds

        if not goal:
            self._set_headers(status=400)
            self.wfile.write(json.dumps({"error": "goal is required"}).encode())
            return
        try:
            import uuid, threading
            task_id = f"collab-{uuid.uuid4().hex[:8]}"
            COLLAB_RESULTS[task_id] = {"status": "starting"}
            t = threading.Thread(target=_run_collab_session, args=(task_id, goal, budget), daemon=True)
            t.start()
            self._set_headers()
            self.wfile.write(json.dumps({"task_id": task_id, "budget": budget}).encode())
        except Exception as e:
            self._set_headers(status=500)
            self.wfile.write(json.dumps({"error": str(e)}).encode())

    def handle_get_collab_result(self, task_id):
        if not task_id or task_id not in COLLAB_RESULTS:
            self._set_headers(status=404)
            self.wfile.write(json.dumps({"error": "unknown collaboration session"}).encode())
            return
        self._set_headers()
        self.wfile.write(json.dumps(COLLAB_RESULTS[task_id]).encode())

    def handle_post_playground_run(self, body: Dict[str, Any]):
        prompt = (body.get("prompt") or "").strip()
        provider = (body.get("provider") or "anthropic").strip()
        task_id = (body.get("task_id") or "").strip()
        user_confirmed = bool(body.get("user_confirmed", False))

        if not prompt and not task_id:
            self._set_headers(status=400)
            self.wfile.write(json.dumps({"error": "Either prompt or task_id is required"}).encode())
            return

        try:
            from council.app import CouncilSession, _new_task
            
            # 1. Initialize or load task
            if not task_id:
                task_id = _new_task(prompt)
                prompt_to_run = prompt
            else:
                prompt_to_run = prompt if prompt else "Continue execution"

            session = CouncilSession(workspace_path=WORKSPACE_DIR)
            if user_confirmed:
                session._user_confirmed[task_id] = True

            # 2. Execute turn
            try:
                response, cost, tier, route_reason = session.execute_turn(task_id, prompt_to_run, provider=provider)
                
                # Fetch updated transcripts
                from council.memory import MemoryWrapper
                transcripts = []
                memory = MemoryWrapper(workspace_path=WORKSPACE_DIR)
                conn = sqlite3.connect(memory.db_path)
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT turn_index, role, content, cost, model, tier FROM transcripts WHERE task_id = ? ORDER BY id ASC",
                    (task_id,)
                )
                rows = cursor.fetchall()
                conn.close()
                for r in rows:
                    transcripts.append({
                        "turn_index": r[0],
                        "role": r[1],
                        "content": r[2],
                        "model": r[4] if len(r) > 4 else None,
                        "tier": r[5] if len(r) > 5 else None,
                        "cost": r[3]
                    })

                self._set_headers()
                self.wfile.write(json.dumps({
                    "status": "success",
                    "task_id": task_id,
                    "response": response,
                    "cost": cost,
                    "tier": tier,
                    "route_reason": route_reason,
                    "transcripts": transcripts
                }).encode())
            except PermissionError as pe:
                if "ENDGAME" in str(pe):
                    self._set_headers()
                    self.wfile.write(json.dumps({
                        "status": "endgame",
                        "task_id": task_id,
                        "message": str(pe)
                    }).encode())
                else:
                    self._set_headers(status=400)
                    self.wfile.write(json.dumps({
                        "status": "error",
                        "error": f"BUDGET LIMIT HALT: {str(pe)}"
                    }).encode())
            except Exception as inner:
                self._set_headers(status=400)
                self.wfile.write(json.dumps({
                    "status": "error",
                    "error": str(inner)
                }).encode())
        except Exception as e:
            self._set_headers(status=500)
            self.wfile.write(json.dumps({"error": str(e)}).encode())

    def handle_post_playground_close(self, body: Dict[str, Any]):
        task_id = (body.get("task_id") or "").strip()
        last_tier = (body.get("last_tier") or "explorer").strip()
        provider = (body.get("provider") or "anthropic").strip()

        if not task_id:
            self._set_headers(status=400)
            self.wfile.write(json.dumps({"error": "task_id is required"}).encode())
            return

        try:
            from council.app import CouncilSession
            session = CouncilSession(workspace_path=WORKSPACE_DIR)
            
            # Generate handoff & close task
            session.generate_and_save_handoff(task_id, last_tier=last_tier, provider=provider)
            session.close_task(task_id)

            self._set_headers()
            self.wfile.write(json.dumps({"status": "success", "message": f"Task {task_id} closed and git boundary committed."}).encode())
        except Exception as e:
            self._set_headers(status=500)
            self.wfile.write(json.dumps({"error": str(e)}).encode())

    def handle_get_models_config(self):
        try:
            from council.ledger import Ledger
            ledger = Ledger(filepath=os.path.join(WORKSPACE_DIR, "ledger_store.json"), pricing_config=os.path.join(WORKSPACE_DIR, "models.json"))
            available_models = sorted(list(ledger.pricing.keys()))
            pricing_info = {}
            for name, details in ledger.pricing.items():
                pricing_info[name] = {
                    "provider": details.get("provider", "anthropic"),
                    "input": details.get("input", 0.0),
                    "output": details.get("output", 0.0),
                    "bench_score": details.get("bench_score"),
                    "bench_total": details.get("bench_total"),
                    "bench_cost": details.get("bench_cost")
                }
            
            self._set_headers()
            self.wfile.write(json.dumps({
                "available_models": available_models,
                "pricing": pricing_info,
                "tier_models": ledger.tier_models,
                "tier_thinking": ledger.tier_thinking,
                "custom_providers": ledger.custom_providers
            }).encode())
        except Exception as e:
            self._set_headers(status=500)
            self.wfile.write(json.dumps({"error": str(e)}).encode())

    def handle_post_models_config(self, body: Dict[str, Any]):
        try:
            tier_models = body.get("tier_models", {})
            tier_thinking = body.get("tier_thinking", {})
            
            models_file = os.path.join(WORKSPACE_DIR, "models.json")
            if os.path.exists(models_file):
                with open(models_file, "r") as f:
                    cfg = json.load(f)
            else:
                cfg = {}
                
            cfg["tier_models"] = tier_models
            cfg["tier_thinking"] = tier_thinking
            
            with open(models_file, "w") as f:
                json.dump(cfg, f, indent=4)
                
            self._set_headers()
            self.wfile.write(json.dumps({"status": "success", "message": "Model mapping config updated successfully."}).encode())
        except Exception as e:
            self._set_headers(status=500)
            self.wfile.write(json.dumps({"error": str(e)}).encode())

    def handle_post_models_probe(self, body: Dict[str, Any]):
        try:
            provider = (body.get("provider") or "").strip()
            model_name = (body.get("model") or "").strip()
            
            if not provider or not model_name:
                self._set_headers(status=400)
                self.wfile.write(json.dumps({"error": "provider and model are required"}).encode())
                return
                
            from council.ledger import Ledger
            from council.client import UnifiedClient
            from council.probe_suite import run_model_probes
            
            ledger = Ledger(filepath=os.path.join(WORKSPACE_DIR, "ledger_store.json"), pricing_config=os.path.join(WORKSPACE_DIR, "models.json"))
            client = UnifiedClient(ledger)
            
            probe_results = run_model_probes(client, provider, model_name)
            
            # Save proposed tier to models.json mapping
            proposed_tier = probe_results["proposed_tier"]
            models_file = os.path.join(WORKSPACE_DIR, "models.json")
            if os.path.exists(models_file):
                with open(models_file, "r") as f:
                    cfg = json.load(f)
            else:
                cfg = {}
                
            cfg.setdefault("tier_models", {}).setdefault(provider, {})[proposed_tier] = model_name
            cfg.setdefault("pricing", {}).setdefault(model_name, {
                "provider": provider,
                "input": 0.15,
                "output": 0.60,
                "verified": False,
                "score": probe_results["score"]
            })
            
            with open(models_file, "w") as f:
                json.dump(cfg, f, indent=4)
                
            self._set_headers()
            self.wfile.write(json.dumps({
                "status": "success",
                "score": probe_results["score"],
                "total_tasks": probe_results["total_tasks"],
                "proposed_tier": proposed_tier,
                "provisional_status": probe_results["provisional_status"] if "provisional_status" in probe_results else probe_results.get("status", "provisional"),
                "results": probe_results["results"]
            }).encode())
        except Exception as e:
            self._set_headers(status=500)
            self.wfile.write(json.dumps({"error": str(e)}).encode())

    def handle_post_models_bench(self, body: Dict[str, Any]):
        try:
            repetitions = int(body.get("repetitions", 2))
            
            from council.ledger import Ledger
            from council.client import UnifiedClient
            from council.council_bench import run_benchmark_and_save
            
            models_file = os.path.join(WORKSPACE_DIR, "models.json")
            ledger = Ledger(filepath=os.path.join(WORKSPACE_DIR, "ledger_store.json"), pricing_config=models_file)
            client = UnifiedClient(ledger)
            
            bench_results = run_benchmark_and_save(client, models_file, repetitions=repetitions)
            
            self._set_headers()
            self.wfile.write(json.dumps({
                "status": "success",
                "results": bench_results["results"],
                "recommendations": bench_results["recommendations"]
            }).encode())
        except Exception as e:
            self._set_headers(status=500)
            self.wfile.write(json.dumps({"error": str(e)}).encode())

    # ========================================================
    # Static Files Handlers
    # ========================================================
    def handle_static_file(self, path: str):
        if path == "/" or not path:
            filepath = os.path.join(STATIC_DIR, "index.html")
        else:
            # Strip leading slash
            filename = path.lstrip("/")
            filepath = os.path.join(STATIC_DIR, filename)

        # Basic directory traversal security check
        if not os.path.abspath(filepath).startswith(os.path.abspath(STATIC_DIR)):
            self._set_headers(status=403)
            self.wfile.write(b"Forbidden")
            return

        if not os.path.exists(filepath) or os.path.isdir(filepath):
            self._set_headers(status=404)
            self.wfile.write(b"Not Found")
            return

        # Determine Content-Type
        content_type = "text/plain"
        if filepath.endswith(".html"):
            content_type = "text/html; charset=utf-8"
        elif filepath.endswith(".css"):
            content_type = "text/css; charset=utf-8"
        elif filepath.endswith(".js"):
            content_type = "application/javascript; charset=utf-8"
        elif filepath.endswith(".png"):
            content_type = "image/png"
        elif filepath.endswith(".jpg") or filepath.endswith(".jpeg"):
            content_type = "image/jpeg"
        elif filepath.endswith(".json"):
            content_type = "application/json"
        elif filepath.endswith(".woff2"):
            content_type = "font/woff2"
        elif filepath.endswith(".woff"):
            content_type = "font/woff"
        elif filepath.endswith(".ttf"):
            content_type = "font/ttf"

        try:
            with open(filepath, "rb") as f:
                content = f.read()
            self._set_headers(content_type=content_type)
            self.wfile.write(content)
        except Exception as e:
            self._set_headers(status=500)
            self.wfile.write(str(e).encode())
    def handle_get_keys(self):
        try:
            anthropic = os.environ.get("ANTHROPIC_API_KEY", "")
            gemini = os.environ.get("GEMINI_API_KEY", "")
            openai = os.environ.get("OPENAI_API_KEY", "")
            
            core_keys = {"ANTHROPIC_API_KEY", "GEMINI_API_KEY", "OPENAI_API_KEY"}
            custom_list = []
            
            # Read from os.environ
            for k, v in os.environ.items():
                if k.endswith("_API_KEY") and k not in core_keys:
                    display_name = k[:-8]
                    custom_list.append({
                        "name": display_name,
                        "key_set": True,
                        "masked": "••••••••••••" + v[-4:] if v else ""
                    })

            # Read from .env file to catch non-loaded env keys
            env_path = os.path.join(WORKSPACE_DIR, ".env")
            if os.path.exists(env_path):
                try:
                    with open(env_path, "r") as f:
                        for line in f:
                            stripped = line.strip()
                            if stripped and not stripped.startswith("#") and "=" in stripped:
                                k, v = stripped.split("=", 1)
                                k = k.strip()
                                v = v.strip()
                                if k.endswith("_API_KEY") and k not in core_keys:
                                    display_name = k[:-8]
                                    if not any(item["name"] == display_name for item in custom_list):
                                        custom_list.append({
                                            "name": display_name,
                                            "key_set": True,
                                            "masked": "••••••••••••" + v[-4:] if v else ""
                                        })
                except Exception:
                    pass

            response = {
                "anthropic_key_set": bool(anthropic),
                "gemini_key_set": bool(gemini),
                "openai_key_set": bool(openai),
                "anthropic_masked": "••••••••••••" + anthropic[-4:] if anthropic else "",
                "gemini_masked": "••••••••••••" + gemini[-4:] if gemini else "",
                "openai_masked": "••••••••••••" + openai[-4:] if openai else "",
                "custom_keys": custom_list
            }
            self._set_headers()
            self.wfile.write(json.dumps(response).encode())
        except Exception as e:
            self._set_headers(status=500)
            self.wfile.write(json.dumps({"error": str(e)}).encode())

    def handle_post_keys(self, body: Dict[str, Any]):
        anthropic = (body.get("anthropic_key") or "").strip()
        gemini = (body.get("gemini_key") or "").strip()
        openai = (body.get("openai_key") or "").strip()
        custom_keys = body.get("custom_keys") or {}

        try:
            # Update current process variables
            if anthropic:
                os.environ["ANTHROPIC_API_KEY"] = anthropic
            if gemini:
                os.environ["GEMINI_API_KEY"] = gemini
            if openai:
                os.environ["OPENAI_API_KEY"] = openai

            # Read existing .env
            env_path = os.path.join(WORKSPACE_DIR, ".env")
            existing = {}
            if os.path.exists(env_path):
                try:
                    with open(env_path, "r") as f:
                        for line in f:
                            stripped = line.strip()
                            if stripped and not stripped.startswith("#") and "=" in stripped:
                                parts = stripped.split("=", 1)
                                existing[parts[0].strip()] = parts[1].strip()
                except Exception:
                    pass

            # Update core keys
            if anthropic:
                existing["ANTHROPIC_API_KEY"] = anthropic
            if gemini:
                existing["GEMINI_API_KEY"] = gemini
            if openai:
                existing["OPENAI_API_KEY"] = openai

            core_keys = {"ANTHROPIC_API_KEY", "GEMINI_API_KEY", "OPENAI_API_KEY"}
            posted_env_keys = {}
            for name, val in custom_keys.items():
                name_clean = name.strip().upper().replace(" ", "_")
                if not name_clean:
                    continue
                env_key = f"{name_clean}_API_KEY" if not name_clean.endswith("_API_KEY") else name_clean
                posted_env_keys[env_key] = val.strip()

            # Remove keys that are custom keys but NOT in the posted custom_keys list
            for k in list(existing.keys()):
                if k not in core_keys and k.endswith("_API_KEY"):
                    if k not in posted_env_keys:
                        existing.pop(k, None)
                        os.environ.pop(k, None)

            # Update and load new custom keys
            for env_key, val in posted_env_keys.items():
                if val: # Only save if a value is provided
                    existing[env_key] = val
                    os.environ[env_key] = val

            with open(env_path, "w") as f:
                f.write("# Local Environment configuration - Council of AIs\n")
                for k, v in sorted(existing.items()):
                    f.write(f"{k}={v}\n")

            self._set_headers()
            self.wfile.write(json.dumps({"status": "success", "message": "Keys saved and loaded successfully."}).encode())
        except Exception as e:
            self._set_headers(status=500)
            self.wfile.write(json.dumps({"error": str(e)}).encode())

def _run_collab_session(task_id: str, goal: str, budget: float):
    try:
        from council.app import CouncilSession
        from council.collaboration import CollaborationSession
        session = CouncilSession(workspace_path=WORKSPACE_DIR)
        collab = CollaborationSession(
            client=session.client,
            memory=session.memory,
            task_id=task_id,
            session_budget=budget,
            escalator=session.escalator,
        )
        COLLAB_RESULTS[task_id] = {"status": "running"}
        result = collab.run(goal)
        COLLAB_RESULTS[task_id] = result
        try:
            session.close_task(task_id)
        except Exception:
            pass
    except Exception as e:
        COLLAB_RESULTS[task_id] = {"status": "error", "error": str(e)}


def run_server(port: int = 8080):
    os.makedirs(STATIC_DIR, exist_ok=True)
    server_address = ("127.0.0.1", port)
    httpd = HTTPServer(server_address, DashboardHTTPHandler)
    print(f"========================================================")
    print(f"Council of AIs Dashboard Server running at:")
    print(f"http://localhost:{port}/")
    print(f"========================================================")
    
    # Auto-open browser
    try:
        webbrowser.open(f"http://localhost:{port}/")
    except Exception:
        pass

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping dashboard server...")
        httpd.server_close()
