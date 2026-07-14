import os
import json
import secrets
import hashlib
import subprocess
from typing import List, Dict, Any, Tuple
from council.escalation import CompletionGate, CleanRoomEscalator

class VerificationRunner:
    def __init__(self, workspace_path: str = "."):
        self.workspace_path = workspace_path
        self.token_file = os.path.join(workspace_path, ".council", "verifier_tokens.json")
        os.makedirs(os.path.dirname(self.token_file), exist_ok=True)
        self.completion_gate = CompletionGate(workspace_path)
        self.escalator = None # initialized lazily if we need escalation
        self.last_escalation_response = None

    def _load_tokens(self) -> Dict[str, str]:
        if os.path.exists(self.token_file):
            try:
                with open(self.token_file, "r") as f:
                    return json.load(f)
            except Exception:
                pass
        return {}

    def _save_tokens(self, tokens: Dict[str, str]):
        with open(self.token_file, "w") as f:
            json.dump(tokens, f, indent=2)

    def create_for_task(self, task_id: str, criteria: List[str]) -> str:
        """
        Creates the completion gate and stores the verification token securely.
        Returns the raw token (which must never be placed in LLM context).
        """
        # Create completion gate -> returns raw token
        token = self.completion_gate.create_gate(task_id, criteria)
        
        # Store hash of token mapped by task_id
        tokens = self._load_tokens()
        tokens[task_id] = token
        self._save_tokens(tokens)
        
        return token

    def run_checks(
        self,
        task_id: str,
        client,
        pre_attempt_snapshot: str,
        pre_attempt_decisions: str,
        original_prompt: str,
        failed_output: str,
        provider: str = "anthropic"
    ) -> Tuple[bool, str]:
        """
        Executes each criterion in the gate checklist.
        - Shell commands (prefixed with 'cmd:') run in subprocess.
        - Text descriptions are graded via a cheap LLM.
        Escalates via CleanRoomEscalator on the first failure.
        """
        tokens = self._load_tokens()
        token = tokens.get(task_id)
        if not token:
            return False, f"No verification token found for task: {task_id}"

        # Fetch current items in gate
        filepath = self.completion_gate._get_filepath(task_id)
        if not os.path.exists(filepath):
            return False, f"No completion gate found for task: {task_id}"

        with open(filepath, "r") as f:
            lines = f.readlines()

        for line in lines:
            line_str = line.strip()
            if not line_str.startswith("- [ ]"):
                continue # Already completed or invalid line shape
            
            criterion = line_str[5:].strip()
            is_passed = False
            error_message = ""

            # Check if it is a command criterion
            if criterion.startswith("cmd:"):
                cmd = criterion[4:].strip()
                try:
                    from council.sandbox import run_sandboxed
                    rc, stdout, stderr = run_sandboxed(cmd, workdir=self.workspace_path, timeout=30)
                    if rc == 0:
                        is_passed = True
                    else:
                        error_message = f"Command exit status {rc}. Stderr: {stderr.strip()}"
                except Exception as e:
                    error_message = f"Command execution exception: {str(e)}"
            else:
                # Text description: grade via cheap LLM call
                if client.is_simulated:
                    # In simulation mode, fail if output doesn't contain a positive marker
                    is_passed = "error" not in failed_output.lower() and "fail" not in failed_output.lower()
                    if not is_passed:
                        error_message = f"Simulated check failed on description: {criterion}"
                else:
                    prompt = (
                        f"Task Output Under Review:\n\"\"\"\n{failed_output}\n\"\"\"\n\n"
                        f"Criterion to verify: \"{criterion}\"\n\n"
                        "Does the Task Output Under Review fully satisfy the verification criterion?\n"
                        "Reply ONLY with '1' for Yes, or '0' for No. Do not add anything else."
                    )
                    from council.client import CacheEnvelope
                    env = CacheEnvelope("You are a verification grader.", "", "")
                    try:
                        grade_res, _ = client.execute_task(
                            task_id=f"verify-grade-{task_id}",
                            tier="cheap",
                            provider=provider,
                            cache_envelope=env,
                            user_prompt=prompt,
                            warm_cache=False
                        )
                        if "1" in grade_res:
                            is_passed = True
                        else:
                            error_message = f"Model graded criterion as failed."
                    except Exception as e:
                        error_message = f"LLM grading call failed: {str(e)}"

            if is_passed:
                # Check off the item
                self.completion_gate.check_off_item(task_id, criterion, token)
            else:
                # First failure -> escalate directly
                escalation_note = "escalated clean-room to Thinker"
                try:
                    escalator = CleanRoomEscalator(client)
                    self.last_escalation_response = escalator.package_and_escalate(
                        task_id=task_id,
                        original_prompt=original_prompt,
                        pre_attempt_snapshot=pre_attempt_snapshot,
                        failed_output=failed_output,
                        provider=provider,
                        pre_attempt_decisions=pre_attempt_decisions
                    )
                except PermissionError as pe:
                    # Budget/endgame block: verification still failed — report
                    # the blocked escalation instead of crashing the runner.
                    escalation_note = f"escalation BLOCKED by ledger: {pe}"
                self._log_outcome(task_id, "failed")
                return False, f"Verification failed on: '{criterion}' ({escalation_note}). Reason: {error_message}"

        self._log_outcome(task_id, "passed")
        return True, "All criteria passed verification successfully."

    def _log_outcome(self, task_id: str, result: str):
        """Real verification outcomes are the flywheel's training labels (§10)."""
        try:
            from council.analytics import AnalyticsLogger
            AnalyticsLogger(self.workspace_path).log_turn(
                task_id=task_id,
                prompt_features=[],
                gate_fired="verification",
                tier="verifier",
                cost=0.0,
                override_used=False,
                verification_result=result
            )
        except Exception:
            pass
