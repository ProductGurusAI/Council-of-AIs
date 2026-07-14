import os
import json
from typing import Dict, Any, Tuple, List

# ---------------------------------------------------------------------------
# Pricing per 1,000,000 tokens.
#
# PRD §11 (metering drift): prices and model IDs MUST live in config, not code.
# This dict is only the shipped default. On first run it is written to
# `models.json` next to the ledger store; edit that file to update prices or
# add models — the Ledger always prefers the config file over these defaults.
#
# Anthropic cache economics: cache WRITE = 1.25x input rate,
# cache READ = 0.10x input rate (90% discount). Entries that omit
# cache_write/cache_read get them derived from the input rate using the
# provider rule, so adding a new model only requires input/output prices.
#
# NOTE: verify prices against provider pricing pages before funding a real
# budget — entries carry "verified" so the dashboard can warn on stale ones.
# ---------------------------------------------------------------------------
DEFAULT_MODEL_PRICING = {
    # --- Anthropic (current generation) ---
    "claude-opus-4-8":            {"provider": "anthropic", "input": 15.00, "output": 75.00, "verified": False},
    "claude-sonnet-5":            {"provider": "anthropic", "input": 3.00,  "output": 15.00, "verified": False},
    "claude-haiku-4-5-20251001":  {"provider": "anthropic", "input": 1.00,  "output": 5.00,  "verified": False},
    # --- Anthropic (legacy, kept for old ledger entries) ---
    "claude-3-opus-20240229":     {"provider": "anthropic", "input": 15.00, "output": 75.00, "verified": True},
    "claude-3-5-sonnet-20240620": {"provider": "anthropic", "input": 3.00,  "output": 15.00, "verified": True},
    "claude-3-haiku-20240307":    {"provider": "anthropic", "input": 0.25,  "output": 1.25,  "verified": True},
    # --- OpenAI ---
    "gpt-4o":      {"provider": "openai", "input": 5.00,  "output": 15.00, "verified": True},
    "gpt-4o-mini": {"provider": "openai", "input": 0.150, "output": 0.600, "verified": True},
    # --- Google Gemini ---
    "gemini-3.5-pro":   {"provider": "gemini", "input": 1.25,  "output": 5.00,  "verified": True},
    "gemini-3.5-flash": {"provider": "gemini", "input": 0.075, "output": 0.300, "verified": True},
}

# Tier -> model mapping, also overridable via models.json ("tier_models" key).
DEFAULT_TIER_MODELS = {
    "anthropic": {"thinker": "claude-opus-4-8", "explorer": "claude-sonnet-5", "cheap": "claude-haiku-4-5-20251001"},
    "openai":    {"thinker": "gpt-4o",          "explorer": "gpt-4o-mini",     "cheap": "gpt-4o-mini"},
    "gemini":    {"thinker": "gemini-3.5-pro",  "explorer": "gemini-3.5-flash","cheap": "gemini-3.5-flash"},
}

# Cache price derivation rules per provider: (write multiplier, read multiplier) on input rate.
CACHE_RULES = {
    "anthropic": (1.25, 0.10),  # explicit cache_control; write premium, 90% read discount
    "openai":    (1.00, 0.50),  # automatic prefix caching; no write premium, 50% read discount
    "gemini":    (1.00, 0.25),  # context caching; storage fee not modeled here
}


def _derive_cache_prices(pricing: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Fills in cache_write / cache_read from provider rules when absent."""
    for model, rates in pricing.items():
        provider = rates.get("provider", "openai")
        write_mult, read_mult = CACHE_RULES.get(provider, (1.0, 1.0))
        rates.setdefault("cache_write", round(rates["input"] * write_mult, 6))
        rates.setdefault("cache_read", round(rates["input"] * read_mult, 6))
    return pricing


# Module-level view for callers that import MODEL_PRICING directly.
MODEL_PRICING = _derive_cache_prices(json.loads(json.dumps(DEFAULT_MODEL_PRICING)))

class Ledger:
    def __init__(self, filepath: str = "ledger_store.json", total_budget: float = None, reserve_floor: float = 10.00, per_task_cap: float = 3.00, pricing_config: str = "models.json"):
        self.filepath = filepath
        self.reserve_floor = reserve_floor
        self.per_task_cap = per_task_cap

        # Config-driven pricing & tier maps (PRD §11: prices in config, not code)
        self.pricing_config = pricing_config
        self.pricing, self.tier_models, self.tier_thinking, self.custom_providers = self._load_pricing_config()

        self.explicit_budget_passed = total_budget is not None
        if total_budget is None:
            try:
                total_budget = float(os.environ.get("TOTAL_BUDGET", "75.00"))
            except (ValueError, TypeError):
                total_budget = 75.00
        self.total_budget = total_budget

        # Load or initialize the ledger data
        self.data = self._load_ledger()

    def _load_pricing_config(self) -> Tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
        """Loads models.json if present; otherwise writes defaults for the user to edit."""
        pricing = json.loads(json.dumps(DEFAULT_MODEL_PRICING))
        tier_models = json.loads(json.dumps(DEFAULT_TIER_MODELS))
        tier_thinking = {
            "thinker": "medium",
            "explorer": "low",
            "cheap": "low",
            "leader": "low"
        }
        custom_providers = {}
        if self.pricing_config and os.path.exists(self.pricing_config):
            try:
                with open(self.pricing_config, "r") as f:
                    cfg = json.load(f)
                pricing.update(cfg.get("pricing", {}))
                for prov, tiers in cfg.get("tier_models", {}).items():
                    if isinstance(tiers, dict):
                        tier_models.setdefault(prov, {}).update(tiers)
                    else:
                        tier_models[prov] = tiers
                if "tier_thinking" in cfg:
                    tier_thinking.update(cfg["tier_thinking"])
                if "custom_providers" in cfg:
                    custom_providers.update(cfg["custom_providers"])
            except (json.JSONDecodeError, OSError):
                pass  # fall back to defaults; never brick the ledger on bad config
        elif self.pricing_config:
            try:
                with open(self.pricing_config, "w") as f:
                    json.dump({"pricing": pricing, "tier_models": tier_models, "tier_thinking": tier_thinking, "custom_providers": custom_providers}, f, indent=4)
            except OSError:
                pass
        return _derive_cache_prices(pricing), tier_models, tier_thinking, custom_providers

    def get_unverified_models(self) -> List[str]:
        """Models whose prices haven't been human-verified — surface on the dashboard."""
        return [m for m, r in self.pricing.items() if not r.get("verified", False)]

    def _load_ledger(self) -> Dict[str, Any]:
        env_budget = None
        if not self.explicit_budget_passed:
            try:
                env_val = os.environ.get("TOTAL_BUDGET")
                if env_val is not None:
                    env_budget = float(env_val)
            except (ValueError, TypeError):
                pass

        if os.path.exists(self.filepath):
            try:
                with open(self.filepath, "r") as f:
                    data = json.load(f)
                # If env_budget is set and is different, automatically update the ledger configuration
                if env_budget is not None and data.get("total_budget") != env_budget:
                    data["total_budget"] = env_budget
                    self._save_ledger(data)
                return data
            except json.JSONDecodeError:
                pass
        
        # Default initialization structure
        initial_budget = env_budget if env_budget is not None else self.total_budget
        initial_structure = {
            "total_budget": initial_budget,
            "reserve_floor": self.reserve_floor,
            "per_task_cap": self.per_task_cap,
            "total_spent": 0.0,
            "tasks": {}
        }
        self._save_ledger(initial_structure)
        return initial_structure

    def _save_ledger(self, data: Dict[str, Any] = None):
        if data is None:
            data = self.data
        with open(self.filepath, "w") as f:
            json.dump(data, f, indent=4)

    def get_remaining_budget(self) -> float:
        return max(0.0, self.data["total_budget"] - self.data["total_spent"])

    def get_task_spend(self, task_id: str) -> float:
        task = self.data["tasks"].get(task_id, {})
        return task.get("total_spent", 0.0)

    def calculate_cost(self, model: str, input_tokens: int, output_tokens: int, cache_write_tokens: int = 0, cache_read_tokens: int = 0) -> float:
        rates = self.pricing.get(model)
        if not rates:
            # Unknown model: fail-expensive. Bill at the priciest known rates so an
            # unrecognized model can never silently under-meter the budget.
            rates = max(self.pricing.values(), key=lambda r: r["input"])
        
        # Standard input/output tokens cost
        # Since rates are per 1M tokens, divide tokens by 1,000,000
        cost = (
            (input_tokens * rates["input"]) +
            (output_tokens * rates["output"]) +
            (cache_write_tokens * rates["cache_write"]) +
            (cache_read_tokens * rates["cache_read"])
        ) / 1000000.0
        return cost

    def record_spend(self, task_id: str, provider: str, model: str, input_tokens: int, output_tokens: int, cache_write_tokens: int = 0, cache_read_tokens: int = 0) -> float:
        cost = self.calculate_cost(model, input_tokens, output_tokens, cache_write_tokens, cache_read_tokens)
        
        # Update total spend
        self.data["total_spent"] += cost
        
        # Update task spend
        if task_id not in self.data["tasks"]:
            self.data["tasks"][task_id] = {
                "total_spent": 0.0,
                "transactions": [],
                "loop_history": []
            }
        
        task_data = self.data["tasks"][task_id]
        task_data["total_spent"] += cost
        task_data["transactions"].append({
            "provider": provider,
            "model": model,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_write_tokens": cache_write_tokens,
            "cache_read_tokens": cache_read_tokens,
            "cost": cost
        })
        
        self._save_ledger()
        return cost

    def record_state(self, task_id: str, state_name: str):
        """Records state execution to monitor runaway loops."""
        if task_id not in self.data["tasks"]:
            self.data["tasks"][task_id] = {
                "total_spent": 0.0,
                "transactions": [],
                "loop_history": []
            }
        self.data["tasks"][task_id]["loop_history"].append(state_name)
        self._save_ledger()

    def check_runaway_loop(self, task_id: str) -> bool:
        """Returns True if the last 3 states in the history are identical (meaning no state change progress)."""
        task_data = self.data["tasks"].get(task_id, {})
        history = task_data.get("loop_history", [])
        if len(history) >= 4:
            # Check if last 4 elements are the same state (i.e. 3 transitions back to same state)
            last_four = history[-4:]
            if len(set(last_four)) == 1:
                return True
        return False

    def check_constraints(self, task_id: str, next_step_est_cost: float, is_thinker_escalation: bool, user_confirmed: bool = False) -> Tuple[bool, str]:
        """
        Validates whether execution is allowed under current budget constraints.
        Returns: (is_allowed, reason)
        """
        remaining_budget = self.get_remaining_budget()
        task_spent = self.get_task_spend(task_id)

        # FR-8: Endgame Mode
        # Below $15 remaining, any thinker-tier spend requires user confirmation.
        if remaining_budget < 15.00 and is_thinker_escalation:
            if not user_confirmed:
                return False, f"ENDGAME: confirm required — est ${next_step_est_cost:.4f}, ${remaining_budget:.4f} left"

        # 1. Total budget exhaustion
        if remaining_budget < next_step_est_cost:
            return False, f"Total budget exhausted. Required: ${next_step_est_cost:.4f}, Available: ${remaining_budget:.4f}."

        # 2. Reserve Floor Check
        # If spending next_step_est_cost takes remaining budget below reserve floor
        # and this is NOT a Thinker escalation, reject it.
        post_spend_budget = remaining_budget - next_step_est_cost
        if post_spend_budget < self.reserve_floor and not is_thinker_escalation:
            return False, f"Reserve floor threshold hit. Budget remaining: ${remaining_budget:.4f}. Only Thinker escalations allowed below ${self.reserve_floor:.2f}."

        # 3. Per-task Cap Check
        if task_spent + next_step_est_cost > self.per_task_cap:
            return False, f"Per-task budget cap reached. Spent so far: ${task_spent:.4f}. Estimated next step: ${next_step_est_cost:.4f}. Task cap is ${self.per_task_cap:.2f}."

        return True, "Constraints satisfied"

    def reset_task_history(self, task_id: str):
        if task_id in self.data["tasks"]:
            self.data["tasks"][task_id]["loop_history"] = []
            self._save_ledger()
