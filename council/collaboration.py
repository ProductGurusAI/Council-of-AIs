"""
Collaboration Sessions — the "Council Chamber" protocol.

A structured, BOUNDED working session between the Thinker and an Explorer.
This is deliberately NOT free-form agent chat: unmoderated model-to-model
dialogue burns budget quadratically and degenerates into agreement spirals.
Every message here has a job, a format, and a terminator.

Protocol:
  1. Thinker writes a contract (interfaces, constraints) + verification criteria.
  2. Explorer may ask AT MOST ONE clarifying question; Thinker answers tersely.
  3. Explorer implements against the contract.
  4. Thinker reviews: numbered defects, or "APPROVE" to finish.
  5. Explorer revises. Review loop hard-capped at MAX_REVIEW_ROUNDS.
  6. Not approved by the cap -> clean-room escalation (Thinker finishes alone).

Budget: every call is metered through the Ledger as usual; additionally a
per-session budget cap halts the session mid-loop if exceeded.

All exchanges are appended to the task transcript with model/tier/cost so the
Council Chamber view (and any audit) can replay the full dialogue.
"""

from typing import Optional, Dict, Any
from council.client import CacheEnvelope

TERSE = (
    "Machine-to-machine channel: be terse. Fragments fine. No pleasantries, "
    "no preamble. Keep code, commands, and errors byte-exact."
)


class SessionBudgetExceeded(Exception):
    pass


class SessionApiFailure(Exception):
    pass


API_ERROR_MARKER = "[SIMULATED FALLBACK — API ERROR"
MAX_CONSECUTIVE_API_ERRORS = 2


class CollaborationSession:
    MAX_REVIEW_ROUNDS = 3
    MAX_CLARIFYING_QUESTIONS = 1

    def __init__(self, client, memory, task_id: str, provider: str = "anthropic",
                 session_budget: float = 1.00, escalator=None):
        self.client = client
        self.memory = memory
        self.task_id = task_id
        self.provider = provider
        self.session_budget = session_budget
        self.escalator = escalator
        self.total_cost = 0.0
        self.rounds_used = 0
        self.questions_used = 0
        self._turn = 0
        # Per-tier: a failing Thinker with a healthy Explorer alternates
        # success/failure, so a global counter resets every round and never
        # trips. Seen in production: Thinker 400'd every review round while
        # Explorer succeeded — session burned all 3 rounds reviewing noise.
        self._consecutive_api_errors = {"thinker": 0, "explorer": 0}

    # ------------------------------------------------------------------ utils

    def _log(self, role: str, content: str, cost: float = 0.0,
             model: Optional[str] = None, tier: Optional[str] = None):
        try:
            self.memory.append_transcript(
                self.task_id, self._turn, role, content,
                cost=cost, model=model, tier=tier
            )
        except PermissionError:
            raise  # write lockout must surface
        self._turn += 1

    def _marker(self, text: str):
        self._log("system", text)

    def _call(self, tier: str, system: str, user_prompt: str) -> str:
        """One metered model call; transcript + budget accounting included."""
        if self.total_cost >= self.session_budget:
            raise SessionBudgetExceeded(
                f"Session budget ${self.session_budget:.2f} exhausted "
                f"(spent ${self.total_cost:.4f})."
            )
        env = CacheEnvelope(f"{system}\n{TERSE}", "", f"Collaboration task: {self.task_id}")
        text, cost = self.client.execute_task(
            task_id=self.task_id,
            tier=tier,
            provider=self.provider,
            cache_envelope=env,
            user_prompt=user_prompt,
            warm_cache=False
        )
        self.total_cost += cost
        model = self.client.select_model(tier, self.provider)
        self._log("assistant", text, cost=cost, model=model, tier=tier)

        # API-error fallback text is NOT a contract, implementation, or review.
        # Without this check, a broken key/model loops error-text through all
        # 3 review rounds and then escalates garbage to the Thinker — burning
        # premium tokens to review noise. Halt fast instead; failed calls cost
        # nothing, so halting is free.
        if text.startswith(API_ERROR_MARKER):
            self._consecutive_api_errors[tier] = self._consecutive_api_errors.get(tier, 0) + 1
            if self._consecutive_api_errors[tier] >= MAX_CONSECUTIVE_API_ERRORS:
                raise SessionApiFailure(
                    f"{self._consecutive_api_errors[tier]} consecutive API failures on tier '{tier}' "
                    f"(model '{model}'). Check the API key and model mapping for this tier."
                )
        else:
            self._consecutive_api_errors[tier] = 0
        if self.total_cost > self.session_budget:
            raise SessionBudgetExceeded(
                f"Session budget ${self.session_budget:.2f} exceeded "
                f"(spent ${self.total_cost:.4f})."
            )
        return text

    @staticmethod
    def _is_approved(review_text: str) -> bool:
        first_line = (review_text or "").strip().splitlines()[0].strip().upper() if review_text else ""
        return first_line.startswith("APPROVE")

    # ------------------------------------------------------------------ phases

    def run(self, goal: str) -> Dict[str, Any]:
        """Executes the full protocol. Returns a summary dict; never raises for
        protocol outcomes (budget halt / escalation are statuses, not errors)."""
        result = {
            "task_id": self.task_id, "status": "unknown", "approved": False,
            "rounds_used": 0, "questions_used": 0, "total_cost": 0.0,
            "final_output": "",
        }
        try:
            self._marker(f"[CHAMBER] Session opened. Goal: {goal}")

            # 1. Thinker: contract + criteria
            self._marker("[CHAMBER] Phase 1 — Thinker drafts contract & criteria")
            contract = self._call(
                "thinker",
                "You are the Thinker. Write a precise implementation contract for the goal: "
                "interfaces/signatures, constraints, and a numbered list of verification criteria. "
                "The Explorer implements ONLY what the contract says.",
                f"Goal: {goal}\nWrite the contract and criteria."
            )

            # 2. Explorer: at most one clarifying question
            self._marker("[CHAMBER] Phase 2 — Explorer may ask ONE clarifying question")
            question = self._call(
                "explorer",
                "You are the Explorer. Read the contract. If exactly one thing is ambiguous "
                "enough to block implementation, ask that ONE question. Otherwise reply NONE.",
                f"Contract:\n{contract}\n\nYour one question, or NONE:"
            )
            answer = ""
            if question.strip().upper() not in ("NONE", "NONE.") and self.questions_used < self.MAX_CLARIFYING_QUESTIONS:
                self.questions_used = 1
                answer = self._call(
                    "thinker",
                    "You are the Thinker. Answer the Explorer's clarifying question in <=3 sentences.",
                    f"Contract:\n{contract}\n\nQuestion:\n{question}"
                )

            # 3. Implement
            self._marker("[CHAMBER] Phase 3 — Explorer implements")
            clarification = f"\nClarification: {answer}" if answer else ""
            implementation = self._call(
                "explorer",
                "You are the Explorer. Implement the contract exactly. Output the implementation only.",
                f"Contract:\n{contract}{clarification}\n\nImplement now."
            )

            # 4/5. Review loop
            approved = False
            for round_no in range(1, self.MAX_REVIEW_ROUNDS + 1):
                self.rounds_used = round_no
                self._marker(f"[CHAMBER] Review round {round_no}/{self.MAX_REVIEW_ROUNDS}")
                review = self._call(
                    "thinker",
                    "You are the Thinker reviewing the Explorer's implementation against the contract. "
                    "If it satisfies every criterion reply with first line exactly 'APPROVE'. "
                    "Otherwise reply ONLY a numbered list of concrete defects.",
                    f"Contract:\n{contract}\n\nImplementation:\n{implementation}\n\nVerdict:"
                )
                if self._is_approved(review):
                    approved = True
                    break
                if round_no == self.MAX_REVIEW_ROUNDS:
                    break  # cap reached; do not revise again
                implementation = self._call(
                    "explorer",
                    "You are the Explorer. Fix ONLY the numbered defects. Output the full revised implementation.",
                    f"Contract:\n{contract}\n\nCurrent implementation:\n{implementation}\n\nDefects:\n{review}\n\nRevise."
                )

            result["final_output"] = implementation

            if approved:
                self._marker("[CHAMBER] APPROVED — session complete")
                result["status"] = "approved"
                result["approved"] = True
            else:
                # 6. Cap reached without approval -> clean-room escalation
                self._marker("[CHAMBER] Review cap reached without approval — clean-room escalation")
                if self.escalator is not None:
                    try:
                        final = self.escalator.package_and_escalate(
                            task_id=self.task_id,
                            original_prompt=f"Goal: {goal}\nContract:\n{contract}",
                            pre_attempt_snapshot="[Collaboration session escalation]",
                            failed_output=implementation,
                            provider=self.provider,
                            pre_attempt_decisions=contract,
                        )
                        result["final_output"] = final
                        self._log("assistant", final, model=self.client.select_model("thinker", self.provider), tier="thinker")
                    except PermissionError as pe:
                        self._marker(f"[CHAMBER] Escalation blocked by ledger: {pe}")
                result["status"] = "escalated"

        except SessionApiFailure as saf:
            self._marker(f"[CHAMBER] HALTED — {saf}")
            result["status"] = "api_error"
            result["error"] = str(saf)
        except SessionBudgetExceeded as sbe:
            self._marker(f"[CHAMBER] HALTED — {sbe}")
            result["status"] = "budget_halted"
        except PermissionError as pe:
            self._marker(f"[CHAMBER] HALTED by ledger — {pe}")
            result["status"] = "ledger_halted"

        result["rounds_used"] = self.rounds_used
        result["questions_used"] = self.questions_used
        result["total_cost"] = round(self.total_cost, 6)
        return result
