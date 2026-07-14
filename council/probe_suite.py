import re
import sys
import json
import time
import subprocess
from typing import Dict, Any, List, Tuple
from council.client import UnifiedClient, CacheEnvelope

# Define the 6 objective evaluation tests
PROBE_TESTS = [
    {
        "id": 1,
        "name": "YAML Format Compliance",
        "prompt": "Output a YAML block containing exactly these two keys: 'status' with value 'ready', and 'score' with value '100'. Output ONLY the raw YAML string without any markdown wraps like ```yaml.",
        "eval_fn": lambda text: bool(re.search(r"status:\s*ready", text) and re.search(r"score:\s*100", text))
    },
    {
        "id": 2,
        "name": "Fact Extraction (Date)",
        "prompt": "Log entry: User logged in on 2026-04-12. Action performed: schema update. Session terminated at 15:43.\nQuestion: What is the login date? Output ONLY the date in YYYY-MM-DD format, nothing else.",
        "eval_fn": lambda text: "2026-04-12" in text.strip()
    },
    {
        "id": 3,
        "name": "Fact Extraction (Cost)",
        "prompt": "Ledger entry: Task 5 spent $1.25. Task 6 spent $0.45. Task 7 spent $2.10.\nQuestion: How much did Task 6 spend? Output ONLY the dollar amount without any dollar signs or extra words (e.g. 0.99).",
        "eval_fn": lambda text: "0.45" in text.strip()
    },
    {
        "id": 4,
        "name": "Constraint Instruction",
        "prompt": "Write a response of exactly five words about coding. Output ONLY the five words.",
        "eval_fn": lambda text: len(text.strip().split()) == 5
    },
    {
        "id": 5,
        "name": "Code Execution A (Reverse)",
        "prompt": "Write a python function `reverse_str(s: str) -> str` that returns the reversed string. Output ONLY the valid Python code, no markdown blocks, no commentary.",
        "eval_fn": lambda text: eval_code_test(text, "reverse_str", "hello", "olleh")
    },
    {
        "id": 6,
        "name": "Code Execution B (Double)",
        "prompt": "Write a python function `double_num(n: int) -> int` that returns double the number. Output ONLY the valid Python code, no markdown blocks, no commentary.",
        "eval_fn": lambda text: eval_code_test(text, "double_num", 21, 42)
    }
]

def eval_code_test(code_text: str, func_name: str, test_input: Any, expected: Any) -> bool:
    """
    SECURITY: probe responses come from UNTRUSTED third-party models — the
    probe exists precisely to evaluate unknown providers. Model code must
    NEVER run in the platform process. We run it in a secure containerized sandbox
    using run_sandboxed with a hard 5-second timeout.
    """
    import os
    from council.sandbox import run_sandboxed
    
    clean_code = re.sub(r"```python\s*|```\s*", "", code_text).strip()
    if not clean_code or len(clean_code) > 10_000:
        return False

    # Create harness that executes the code locally and prints JSON results
    harness_append = (
        "\nimport json, sys\n"
        "try:\n"
        f"    result = {func_name}({test_input!r})\n"
        f"    print(json.dumps({{'ok': True, 'result': result}}))\n"
        "except Exception as e:\n"
        "    print(json.dumps({'ok': False, 'result': None, 'error': str(e)}))\n"
    )
    
    filename = f"temp_probe_{func_name}_{int(time.time())}.py"
    with open(filename, "w") as f:
        f.write(clean_code + "\n" + harness_append)
        
    try:
        rc, stdout, stderr = run_sandboxed([sys.executable, "-I", filename], workdir=".", timeout=5)
        if rc != 0:
            return False
        out = json.loads(stdout.strip().splitlines()[-1])
        return bool(out.get("ok")) and out.get("result") == expected
    except Exception:
        return False
    finally:
        if os.path.exists(filename):
            try:
                os.remove(filename)
            except OSError:
                pass

def run_model_probes(client: UnifiedClient, provider: str, model_name: str) -> Dict[str, Any]:
    """
    Executes the 6 fixed probe tasks using a dummy task context.
    Returns details on each test, aggregate score, and proposed tier assignment.
    """
    results = []
    score = 0

    # Ensure model is registered provisionally in pricing so calculations don't fail
    if model_name not in client.ledger.pricing:
        client.ledger.pricing[model_name] = {
            "input_cost_per_token": 0.00001,
            "output_cost_per_token": 0.00003,
            "verified": False
        }

    # Dummy Task ID for probe suite executions
    probe_task_id = f"probe-{provider}-{model_name}-{int(time.time())}"

    for test in PROBE_TESTS:
        # Construct standard cache envelope structure
        envelope = CacheEnvelope(
            system_and_invariants="You are a precise evaluation agent.",
            decisions_and_questions="Verify constraints and follow instructions.",
            task_and_canvas=f"Run test {test['id']}: {test['name']}"
        )

        try:
            # Check simulation or real execution
            if client.is_simulated:
                # Provide simulated responses that satisfy our eval functions
                if test["id"] == 1:
                    response = "status: ready\nscore: 100"
                elif test["id"] == 2:
                    response = "2026-04-12"
                elif test["id"] == 3:
                    response = "0.45"
                elif test["id"] == 4:
                    response = "Python makes building software fun"
                elif test["id"] == 5:
                    response = "def reverse_str(s):\n    return s[::-1]"
                elif test["id"] == 6:
                    response = "def double_num(n):\n    return n * 2"
                cost = 0.001
            else:
                # Real API execute
                response, cost = client.execute_task(
                    task_id=probe_task_id,
                    tier="cheap",
                    provider=provider,
                    cache_envelope=envelope,
                    user_prompt=test["prompt"],
                    warm_cache=False,
                    user_confirmed=True
                )

            passed = test["eval_fn"](response)
            if passed:
                score += 1

            results.append({
                "id": test["id"],
                "name": test["name"],
                "passed": passed,
                "response": response[:100] + ("..." if len(response) > 100 else ""),
                "cost": cost
            })
        except Exception as e:
            results.append({
                "id": test["id"],
                "name": test["name"],
                "passed": False,
                "error": str(e),
                "cost": 0.0
            })

    # Propose tier assignment based on the score
    if score >= 5:
        proposed_tier = "thinker"
    elif score >= 3:
        proposed_tier = "explorer"
    else:
        proposed_tier = "cheap"

    return {
        "score": score,
        "total_tasks": len(PROBE_TESTS),
        "proposed_tier": proposed_tier,
        "status": "provisional — confirmed by usage",
        "results": results
    }
