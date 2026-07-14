import os
import json
import time
import sys
import re
import subprocess
from typing import Dict, Any, List, Tuple, Optional
from council.client import UnifiedClient, CacheEnvelope
from council.ledger import Ledger

# Labeled Cheap Tasks Dataset
CHEAP_DATA = [
    ("Query balance for checking account", "finance"),
    ("Write a python quicksort", "code"),
    ("Who is the president of USA?", "general"),
    ("Extract from 'id: 994'", "994"),
    ("Format: 12 April 2026", "2026-04-12"),
    ("Calculate 3 * 4", "12"),
    ("Reset password for admin", "auth"),
    ("Create user table", "database"),
    ("Optimize SELECT performance", "database"),
    ("Write class tests", "code"),
    ("Translate 'bonjour' to english", "hello"),
    ("What is the Capital of France?", "paris"),
    ("Is it going to rain tomorrow?", "general"),
    ("Parse error code 500", "error"),
    ("Match email address regex", "regex"),
    ("Generate a random UUID", "uuid"),
    ("Merge feature branch into main", "git"),
    ("Commit boundary changes", "git"),
    ("Fetch from external REST API", "api"),
    ("Convert XML structure to JSON", "convert")
]

def get_cheap_prompt_text(i: int) -> str:
    return CHEAP_DATA[i-1][0]

def get_cheap_expected(i: int) -> str:
    return CHEAP_DATA[i-1][1]

# Sandboxed Python Execution Helper
def run_sandboxed_code(code_text: str, test_code: str) -> bool:
    clean_code = re.sub(r"```python\s*|```\s*", "", code_text).strip()
    if not clean_code or len(clean_code) > 10_000:
        return False

    harness = (
        "import json, sys\n"
        "ns = {}\n"
        "try:\n"
        "    exec(sys.stdin.read(), ns, ns)\n"
        f"    {test_code}\n"
        "    print(json.dumps({'ok': True}))\n"
        "except Exception as e:\n"
        "    print(json.dumps({'ok': False, 'error': str(e)}))\n"
    )
    try:
        proc = subprocess.run(
            [sys.executable, "-I", "-c", harness],
            input=clean_code,
            capture_output=True, text=True, timeout=5,
            env={"PATH": ""},
        )
        out = json.loads(proc.stdout.strip().splitlines()[-1])
        return bool(out.get("ok"))
    except Exception:
        return False

# Evaluation functions
def eval_safe_divide(text: str) -> bool:
    test_code = (
        "assert ns.get('safe_divide') is not None, 'safe_divide function not defined'\n"
        "assert ns['safe_divide'](10.0, 2.0) == 5.0, 'Divide failed'\n"
        "assert ns['safe_divide'](5.0, 0.0) is None, 'Edge case b=0 failed'\n"
    )
    return run_sandboxed_code(text, test_code)

def eval_stack(text: str) -> bool:
    test_code = (
        "assert ns.get('Stack') is not None, 'Stack class not defined'\n"
        "s = ns['Stack']()\n"
        "assert s.is_empty(), 'Initial stack should be empty'\n"
        "s.push(42)\n"
        "assert not s.is_empty(), 'Stack should not be empty after push'\n"
        "assert s.pop() == 42, 'Pop value mismatch'\n"
        "assert s.is_empty(), 'Stack should be empty after pop'\n"
    )
    return run_sandboxed_code(text, test_code)

# Benchmark Tasks Definitions
THINKER_TASKS = [
    {
        "id": "thinker_safe_divide",
        "name": "Hidden Edge-Case Code",
        "prompt": "Write a python function `safe_divide(a: float, b: float) -> Optional[float]` that returns None if b is 0, otherwise a/b. Output ONLY the valid Python code, no markdown blocks, no commentary.",
        "eval_fn": eval_safe_divide
    },
    {
        "id": "thinker_logic",
        "name": "Known-Answer Logic",
        "prompt": "A box contains 3 red balls and 7 blue balls. If you draw two balls at random without replacement, what is the probability that both are red? Output ONLY the answer as a fraction (e.g. 1/15), nothing else.",
        "eval_fn": lambda text: "1/15" in text.strip()
    },
    {
        "id": "thinker_flaw",
        "name": "Planted Flaw Review",
        "prompt": "Identify the security vulnerability in this python code:\n```python\ndef query_db(db, uid):\n    return db.execute('SELECT * FROM users WHERE id = ' + uid)\n```\nOutput ONLY the vulnerability code tag: SQL_INJECTION, XSS, or CSRF.",
        "eval_fn": lambda text: "SQL_INJECTION" in text.strip()
    }
]

EXPLORER_TASKS = [
    {
        "id": "explorer_stack",
        "name": "Interface Implementation",
        "prompt": "Write a python class `Stack` with `push`, `pop`, and `is_empty` methods, and a simple assert checking push and pop. Output ONLY the valid Python code, no markdown blocks, no commentary.",
        "eval_fn": eval_stack
    },
    {
        "id": "explorer_summary",
        "name": "Fact-Check Summarization",
        "prompt": "Summarize this log profile:\n- Server started at 08:00 UTC\n- Database connected at 08:02 UTC\n- API routing initialized at 08:05 UTC\n- Memory check: 85% free\nMake sure your summary contains the exact words: started, connected, initialized, and free. Output ONLY your summary.",
        "eval_fn": lambda text: all(word in text.lower() for word in ["started", "connected", "initialized", "free"])
    }
]

CHEAP_TASKS = []
for idx in range(1, 21):
    CHEAP_TASKS.append({
        "id": f"cheap_task_{idx}",
        "name": f"Cheap Task {idx}",
        "prompt": f"Classify intent or extract values from this text: '{get_cheap_prompt_text(idx)}'. Output ONLY the exact target label: '{get_cheap_expected(idx)}', nothing else.",
        "eval_fn": lambda text, i=idx: get_cheap_expected(i) in text.strip()
    })

# Testing Mock Response Overrides
MOCK_BENCH_ANSWERS: Dict[Tuple[str, str, str], str] = {}

def get_simulated_response(task_id: str) -> str:
    if task_id == "thinker_safe_divide":
        return "def safe_divide(a, b):\n    return None if b == 0 else a/b"
    elif task_id == "thinker_logic":
        return "1/15"
    elif task_id == "thinker_flaw":
        return "SQL_INJECTION"
    elif task_id == "explorer_stack":
        return "class Stack:\n    def __init__(self):\n        self.items = []\n    def push(self, x):\n        self.items.append(x)\n    def pop(self):\n        return self.items.pop()\n    def is_empty(self):\n        return len(self.items) == 0"
    elif task_id == "explorer_summary":
        return "started, connected, initialized, free"
    elif task_id.startswith("cheap_task_"):
        i = int(task_id.split("_")[-1])
        return get_cheap_expected(i)
    return "pass"

def run_benchmark(client: UnifiedClient, repetitions: int = 2) -> Dict[str, Any]:
    """
    Runs the multi-provider benchmark suite.
    Evaluates each configured provider × tier models × repetitions.
    All transactions are metered under task ID 'instrumentation'.
    """
    # Find active providers (configured or custom)
    providers = ["anthropic", "openai", "gemini"]
    for cp in client.ledger.custom_providers:
        if cp not in providers:
            providers.append(cp)

    # Filter out providers that don't have tier mapping defined
    active_providers = []
    for p in providers:
        if p in client.ledger.tier_models or any(details.get("provider") == p for details in client.ledger.pricing.values()):
            active_providers.append(p)

    results = {}
    
    for provider in active_providers:
        results[provider] = {}
        
        # Tiers evaluation
        for tier, tasks in [("thinker", THINKER_TASKS), ("explorer", EXPLORER_TASKS), ("cheap", CHEAP_TASKS)]:
            try:
                # Find tier model name for this provider
                model_name = client.ledger.tier_models.get(provider, {}).get(tier)
                if not model_name:
                    # Fallback to checking pricing catalog
                    for m, d in client.ledger.pricing.items():
                        if d.get("provider") == provider:
                            model_name = m
                            break
                if not model_name:
                    continue

                total_score = 0
                total_cost = 0.0
                total_latency = 0.0
                run_count = 0

                for task in tasks:
                    for _ in range(repetitions):
                        start_time = time.time()
                        
                        mock_key = (provider, model_name, task["id"])
                        if mock_key in MOCK_BENCH_ANSWERS:
                            response_text = MOCK_BENCH_ANSWERS[mock_key]
                            # Force record mock spend
                            cost = client.ledger.record_spend(
                                task_id="instrumentation",
                                provider=provider,
                                model=model_name,
                                input_tokens=100,
                                output_tokens=100
                            )
                        elif client.is_simulated:
                            response_text = get_simulated_response(task["id"])
                            # Record simulated spend
                            cost = client.ledger.record_spend(
                                task_id="instrumentation",
                                provider=provider,
                                model=model_name,
                                input_tokens=100,
                                output_tokens=100
                            )
                        else:
                            # Real API call
                            envelope = CacheEnvelope(
                                system_and_invariants="[BENCHMARK RUN]",
                                decisions_and_questions="Verify and perform task.",
                                task_and_canvas=f"Benchmark: {task['name']}"
                            )
                            response_text, cost = client.execute_task(
                                task_id="instrumentation",
                                tier=tier,
                                provider=provider,
                                cache_envelope=envelope,
                                user_prompt=task["prompt"],
                                warm_cache=False,
                                user_confirmed=True
                            )

                        latency = time.time() - start_time
                        passed = task["eval_fn"](response_text)
                        
                        if passed:
                            total_score += 1
                        total_cost += cost
                        total_latency += latency
                        run_count += 1

                avg_cost = total_cost / run_count if run_count > 0 else 0.0
                avg_latency = total_latency / run_count if run_count > 0 else 0.0
                
                results[provider][tier] = {
                    "model": model_name,
                    "score": total_score,
                    "total": run_count,
                    "cost": avg_cost,
                    "latency": avg_latency
                }
            except Exception as e:
                # Log provider-tier error gracefully, do not crash benchmark run
                print(f"Error benchmarking provider {provider} tier {tier}: {e}")

    # Optimize and compute recommendations
    recommendations = {}
    
    # 1. Thinker mapping: top score
    best_thinker_model = None
    best_thinker_score = -1
    for p, tiers in results.items():
        if "thinker" in tiers:
            t_res = tiers["thinker"]
            if t_res["score"] > best_thinker_score:
                best_thinker_score = t_res["score"]
                best_thinker_model = t_res["model"]
    if best_thinker_model:
        recommendations["thinker"] = best_thinker_model

    # 2. Explorer mapping: top score / cost
    best_explorer_model = None
    best_explorer_ratio = -1.0
    for p, tiers in results.items():
        if "explorer" in tiers:
            e_res = tiers["explorer"]
            ratio = e_res["score"] / e_res["cost"] if e_res["cost"] > 0 else e_res["score"]
            if ratio > best_explorer_ratio:
                best_explorer_ratio = ratio
                best_explorer_model = e_res["model"]
    if best_explorer_model:
        recommendations["explorer"] = best_explorer_model

    # 3. Cheap mapping: top score (accuracy) with latency tiebreaker
    best_cheap_model = None
    best_cheap_score = -1
    best_cheap_latency = float("inf")
    for p, tiers in results.items():
        if "cheap" in tiers:
            c_res = tiers["cheap"]
            if c_res["score"] > best_cheap_score:
                best_cheap_score = c_res["score"]
                best_cheap_latency = c_res["latency"]
                best_cheap_model = c_res["model"]
            elif c_res["score"] == best_cheap_score:
                if c_res["latency"] < best_cheap_latency:
                    best_cheap_latency = c_res["latency"]
                    best_cheap_model = c_res["model"]
    if best_cheap_model:
        recommendations["cheap"] = best_cheap_model

    return {
        "results": results,
        "recommendations": recommendations
    }

def run_benchmark_and_save(client: UnifiedClient, models_path: str, repetitions: int = 2) -> Dict[str, Any]:
    """Runs the benchmark and persists recommendations + scoring metadata to models.json."""
    bench_data = run_benchmark(client, repetitions=repetitions)
    
    if os.path.exists(models_path):
        with open(models_path, "r") as f:
            cfg = json.load(f)
    else:
        cfg = {}

    # Merge recommendations flats
    cfg["tier_models"] = bench_data["recommendations"]

    # Save benchmark details to model pricing index as verified scoring metadata
    pricing = cfg.setdefault("pricing", {})
    for provider, tiers in bench_data["results"].items():
        for tier, metric in tiers.items():
            model_name = metric["model"]
            if model_name in pricing:
                pricing[model_name]["bench_score"] = metric["score"]
                pricing[model_name]["bench_total"] = metric["total"]
                pricing[model_name]["bench_cost"] = metric["cost"]

    # Write back to models.json
    with open(models_path, "w") as f:
        json.dump(cfg, f, indent=4)

    return bench_data
