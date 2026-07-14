import os
import json
import hashlib
import secrets
from typing import List, Dict, Any, Optional


class CompletionGate:
    """
    File-based completion gate (FR-15a).

    Enforcement is by MECHANISM, not convention:
    - create_gate() returns a one-time verify token. Only its SHA-256 is stored.
      The token goes to the verification runner — never to the executing model.
    - check_off_item() requires the token; without it the write is rejected.
    - The checklist item set is hashed at creation; is_complete() re-verifies the
      structure hash so an executor cannot delete or reword criteria to pass.
    """

    def __init__(self, workspace_path: str = "."):
        self.workspace_path = workspace_path
        self.gates_dir = os.path.join(workspace_path, ".council", "completion_gates")
        os.makedirs(self.gates_dir, exist_ok=True)

    def _get_filepath(self, task_id: str) -> str:
        return os.path.join(self.gates_dir, f"{task_id}.gate")

    def _get_metapath(self, task_id: str) -> str:
        return os.path.join(self.gates_dir, f"{task_id}.gate.meta")

    @staticmethod
    def _structure_hash(items: List[str]) -> str:
        canonical = "\n".join(sorted(i.strip() for i in items))
        return hashlib.sha256(canonical.encode()).hexdigest()

    def create_gate(self, task_id: str, items: List[str]) -> str:
        """
        Creates a write-locked completion gate. Returns the verify token that
        MUST be presented to check items off. Store it with the verification
        runner only — the executing model must never see it.
        """
        filepath = self._get_filepath(task_id)
        if os.path.exists(filepath):
            raise PermissionError(f"Gate for task {task_id} already exists and is write-locked.")

        token = secrets.token_hex(16)
        meta = {
            "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
            "structure_sha256": self._structure_hash(items),
            "item_count": len(items),
        }

        with open(filepath, "w") as f:
            for item in items:
                f.write(f"- [ ] {item.strip()}\n")
        with open(self._get_metapath(task_id), "w") as f:
            json.dump(meta, f, indent=2)

        return token

    def _load_meta(self, task_id: str) -> Dict[str, Any]:
        metapath = self._get_metapath(task_id)
        if not os.path.exists(metapath):
            raise FileNotFoundError(f"Gate metadata missing for task {task_id}; gate is untrusted.")
        with open(metapath, "r") as f:
            return json.load(f)

    def _current_items(self, task_id: str) -> List[str]:
        with open(self._get_filepath(task_id), "r") as f:
            lines = f.readlines()
        items = []
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("- [ ] "):
                items.append(stripped[6:])
            elif stripped.startswith("- [x] "):
                items.append(stripped[6:])
        return items

    def verify_structure(self, task_id: str) -> bool:
        """True if the checklist item set is unchanged since creation."""
        meta = self._load_meta(task_id)
        return self._structure_hash(self._current_items(task_id)) == meta["structure_sha256"]

    def check_off_item(self, task_id: str, item_name: str, verify_token: str):
        """Marks a checklist item as completed. Requires the creation-time token."""
        filepath = self._get_filepath(task_id)
        if not os.path.exists(filepath):
            raise FileNotFoundError(f"Completion gate file not found for task: {task_id}")

        meta = self._load_meta(task_id)
        presented = hashlib.sha256((verify_token or "").encode()).hexdigest()
        if presented != meta["token_sha256"]:
            raise PermissionError(
                f"FR-15a: invalid verify token for gate {task_id}. "
                "Only the verification runner holds the token; executors cannot check off their own criteria."
            )
        if not self.verify_structure(task_id):
            raise PermissionError(
                f"FR-15a: gate {task_id} checklist structure was tampered with since creation. Gate is void."
            )

        with open(filepath, "r") as f:
            content = f.read()

        target = f"- [ ] {item_name.strip()}"
        replacement = f"- [x] {item_name.strip()}"

        if target not in content:
            # Check if it was already checked off
            if replacement in content:
                return  # Already checked off, do nothing
            raise ValueError(f"Item '{item_name}' not found in completion gate checklist.")

        content = content.replace(target, replacement, 1)

        with open(filepath, "w") as f:
            f.write(content)

    def is_complete(self, task_id: str) -> bool:
        """True only if every item is checked AND the structure hash still matches."""
        filepath = self._get_filepath(task_id)
        if not os.path.exists(filepath):
            return False
        try:
            if not self.verify_structure(task_id):
                return False  # tampered gates never report complete
        except FileNotFoundError:
            return False

        with open(filepath, "r") as f:
            lines = f.readlines()

        for line in lines:
            line_str = line.strip()
            if line_str.startswith("- [ ]") or "[ ]" in line_str:
                return False
        return True


class CleanRoomEscalator:
    def __init__(self, client):
        self.client = client

    def package_and_escalate(
        self,
        task_id: str,
        original_prompt: str,
        pre_attempt_snapshot: str,
        failed_output: str,
        provider: str = "anthropic",
        pre_attempt_decisions: Optional[str] = None
    ) -> str:
        """
        Packages the clean-room escalation context (FR-18):
        - Original Prompt
        - Pre-attempt clean snapshot (Invariants) in Layer 1
        - Pre-attempt Decisions/Open Questions in Layer 2 (NOT hardcoded away —
          the Thinker needs the real decision state that existed before the
          failed attempt, or it will re-litigate settled choices)
        - Failed attempt labeled explicitly as suspect evidence.
        Routes to the Thinker for deep reasoning.
        """
        escalation_prompt = (
            "A cheaper model attempted the task below and failed verification.\n"
            "Its output is attached below as SUSPECT EVIDENCE. Do NOT anchor to its code or copy its mistakes.\n"
            "Identify the failure, verify constraints, and write a correct implementation.\n\n"
            f"=== ORIGINAL TASK PROMPT ===\n{original_prompt}\n\n"
            f"=== SUSPECT EVIDENCE (FAILED ATTEMPT) ===\n{failed_output}\n"
        )

        # Setup CacheEnvelope containing the pre-attempt clean snapshot.
        # This isolates the Thinker from any poisoning inside the failed attempt.
        from council.client import CacheEnvelope
        env = CacheEnvelope(
            system_and_invariants="[CLEAN-ROOM ESCALATION ROUTE]\n" + pre_attempt_snapshot,
            decisions_and_questions=pre_attempt_decisions or "No prior decisions recorded for this task.",
            task_and_canvas=f"Task ID: {task_id}\nEscalation state: active"
        )

        response, _ = self.client.execute_task(
            task_id=task_id,
            tier="thinker",
            provider=provider,
            cache_envelope=env,
            user_prompt=escalation_prompt,
            warm_cache=False  # Force cache write since this is a clean-room escalation epoch
        )

        return response
