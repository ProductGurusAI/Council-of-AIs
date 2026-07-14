import os
import json
from typing import Dict, Any, List

class AnalyticsLogger:
    def __init__(self, workspace_path: str = "."):
        self.workspace_path = workspace_path
        self.log_file = os.path.join(workspace_path, ".council", "routing_log.jsonl")
        os.makedirs(os.path.dirname(self.log_file), exist_ok=True)

    def log_turn(
        self,
        task_id: str,
        prompt_features: List[str],
        gate_fired: str,
        tier: str,
        cost: float,
        override_used: bool,
        verification_result: str
    ):
        """Appends a single JSON line to the routing_log.jsonl file."""
        entry = {
            "task_id": task_id,
            "prompt_features": prompt_features,
            "gate_fired": gate_fired,
            "tier": tier,
            "cost": cost,
            "override_used": override_used,
            "verification_result": verification_result
        }
        with open(self.log_file, "a") as f:
            f.write(json.dumps(entry) + "\n")

    def get_statistics(self, ledger_data: Dict[str, Any] = None) -> Dict[str, Any]:
        """
        Calculates analytics statistics:
        - Spend per completed task by route
        - Misroute proxies (override count, failed-then-escalated count)
        - Orchestration overhead %
        """
        turns = []
        if os.path.exists(self.log_file):
            try:
                with open(self.log_file, "r") as f:
                    for line in f:
                        line_str = line.strip()
                        if line_str:
                            turns.append(json.loads(line_str))
            except Exception:
                pass

        # 1. Misroute proxies
        override_count = sum(1 for t in turns if t.get("override_used"))
        
        # Track failed-then-escalated count
        # A task is failed-then-escalated if it has a verification_result = "failed"
        failed_tasks = {t["task_id"] for t in turns if t.get("verification_result") == "failed"}
        failed_then_escalated_count = len(failed_tasks)

        # 2. Spend per route (based on turns logged)
        route_spend: Dict[str, float] = {"thinker": 0.0, "explorer": 0.0}
        route_count: Dict[str, int] = {"thinker": 0, "explorer": 0}
        
        # We can group turn costs by their routed tier
        for t in turns:
            tier = t.get("tier", "explorer")
            cost = t.get("cost", 0.0)
            if tier in route_spend:
                route_spend[tier] += cost
                route_count[tier] += 1
            else:
                route_spend["explorer"] += cost
                route_count["explorer"] += 1

        avg_spend = {}
        for tier in route_spend:
            count = route_count[tier]
            avg_spend[tier] = route_spend[tier] / count if count > 0 else 0.0

        # 3. Orchestration overhead percentage
        # Sum cost of all verification/quizzing calls vs writing calls in ledger transactions
        total_spent = 0.0
        orchestration_spent = 0.0
        
        if ledger_data and "tasks" in ledger_data:
            total_spent = ledger_data.get("total_spent", 0.0)
            for task_id, task_val in ledger_data["tasks"].items():
                # Check transactions
                for tx in task_val.get("transactions", []):
                    tx_cost = tx.get("cost", 0.0)
                    # Grader / quiz / verification tasks count as orchestration overhead
                    if (
                        task_id.startswith("quiz-") or 
                        task_id.startswith("verify-grade-") or 
                        "verify-grade" in task_id or
                        "quiz" in task_id
                    ):
                        orchestration_spent += tx_cost

        overhead_pct = (orchestration_spent / total_spent * 100.0) if total_spent > 0 else 0.0

        return {
            "total_turns_logged": len(turns),
            "override_count": override_count,
            "failed_then_escalated_count": failed_then_escalated_count,
            "avg_spend_per_turn_by_route": avg_spend,
            "total_spend_by_route": route_spend,
            "orchestration_overhead_pct": overhead_pct,
            "orchestration_spent": orchestration_spent,
            "total_spent": total_spent
        }
