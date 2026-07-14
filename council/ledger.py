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

        # Resolve DB path
        if self.filepath.endswith(".json"):
            self.db_path = self.filepath[:-5] + ".db"
        else:
            self.db_path = self.filepath

        self._init_db()
        self._migrate_json()
        self._sync_env_budget()

    @property
    def data(self) -> Dict[str, Any]:
        """
        Dynamically construct a compatible dictionary representation of the ledger data from SQLite,
        supporting reads and writes transparently for backward compatibility.
        """
        class LedgerDataProxy(dict):
            def __init__(self, ledger):
                self.ledger = ledger
                super().__init__()
            
            def __getitem__(self, key):
                return self.ledger._get_data_value(key)
                
            def __setitem__(self, key, value):
                self.ledger._set_data_value(key, value)
                
            def get(self, key, default=None):
                try:
                    return self[key]
                except KeyError:
                    return default
                    
            def __contains__(self, key):
                return key in ("total_budget", "reserve_floor", "per_task_cap", "total_spent", "tasks")
        
        return LedgerDataProxy(self)

    def _get_data_value(self, key: str) -> Any:
        import sqlite3
        if key in ("total_budget", "reserve_floor", "per_task_cap", "total_spent"):
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute(f"SELECT {key} FROM ledger_metadata WHERE id = 1")
            row = cursor.fetchone()
            conn.close()
            return row[0] if row else 0.0
            
        elif key == "tasks":
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute("SELECT DISTINCT task_id FROM transactions UNION SELECT DISTINCT task_id FROM loop_history")
            task_ids = [r[0] for r in cursor.fetchall()]
            
            tasks = {}
            for t_id in task_ids:
                cursor.execute("SELECT SUM(cost) FROM transactions WHERE task_id = ?", (t_id,))
                task_spent = cursor.fetchone()[0] or 0.0
                
                cursor.execute("SELECT provider, model, input_tokens, output_tokens, cache_write_tokens, cache_read_tokens, cost FROM transactions WHERE task_id = ?", (t_id,))
                tx_rows = cursor.fetchall()
                tx_list = []
                for tx in tx_rows:
                    tx_list.append({
                        "provider": tx[0],
                        "model": tx[1],
                        "input_tokens": tx[2],
                        "output_tokens": tx[3],
                        "cache_write_tokens": tx[4],
                        "cache_read_tokens": tx[5],
                        "cost": tx[6]
                    })
                    
                cursor.execute("SELECT state_name FROM loop_history WHERE task_id = ? ORDER BY id ASC", (t_id,))
                lh_rows = [r[0] for r in cursor.fetchall()]
                
                tasks[t_id] = {
                    "total_spent": task_spent,
                    "transactions": tx_list,
                    "loop_history": lh_rows
                }
            conn.close()
            return tasks
            
        raise KeyError(key)

    def _set_data_value(self, key: str, value: Any):
        import sqlite3
        if key in ("total_budget", "reserve_floor", "per_task_cap", "total_spent"):
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("BEGIN IMMEDIATE")
                cursor = conn.cursor()
                cursor.execute(f"UPDATE ledger_metadata SET {key} = ? WHERE id = 1", (value,))
                conn.commit()

    def _init_db(self):
        import sqlite3
        os.makedirs(os.path.dirname(os.path.abspath(self.db_path)), exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS ledger_metadata (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                total_budget REAL,
                reserve_floor REAL,
                per_task_cap REAL,
                total_spent REAL
            )
        """)
        
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS reservations (
                id TEXT PRIMARY KEY,
                task_id TEXT,
                est_cost REAL,
                created_at REAL,
                status TEXT
            )
        """)
        
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT,
                provider TEXT,
                model TEXT,
                input_tokens INTEGER,
                output_tokens INTEGER,
                cache_write_tokens INTEGER,
                cache_read_tokens INTEGER,
                cost REAL
            )
        """)
        
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS loop_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT,
                state_name TEXT
            )
        """)
        conn.commit()
        conn.close()

    def _migrate_json(self):
        if not os.path.exists(self.filepath) or not self.filepath.endswith(".json"):
            return
            
        import sqlite3
        try:
            with open(self.filepath, "r") as f:
                data = json.load(f)
        except Exception:
            return
            
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute("SELECT COUNT(*) FROM ledger_metadata")
        if cursor.fetchone()[0] > 0:
            conn.close()
            return
            
        cursor.execute(
            "INSERT INTO ledger_metadata (id, total_budget, reserve_floor, per_task_cap, total_spent) VALUES (1, ?, ?, ?, ?)",
            (data.get("total_budget", self.total_budget), data.get("reserve_floor", self.reserve_floor), data.get("per_task_cap", self.per_task_cap), data.get("total_spent", 0.0))
        )
        
        tasks = data.get("tasks", {})
        for task_id, task_val in tasks.items():
            for tx in task_val.get("transactions", []):
                cursor.execute(
                    "INSERT INTO transactions (task_id, provider, model, input_tokens, output_tokens, cache_write_tokens, cache_read_tokens, cost) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (task_id, tx.get("provider"), tx.get("model"), tx.get("input_tokens", 0), tx.get("output_tokens", 0), tx.get("cache_write_tokens", 0), tx.get("cache_read_tokens", 0), tx.get("cost", 0.0))
                )
            for lh in task_val.get("loop_history", []):
                cursor.execute(
                    "INSERT INTO loop_history (task_id, state_name) VALUES (?, ?)",
                    (task_id, lh)
                )
        conn.commit()
        conn.close()
        
        try:
            os.rename(self.filepath, self.filepath + ".migrated")
        except Exception:
            pass

    def _sync_env_budget(self):
        env_budget = None
        if not self.explicit_budget_passed:
            try:
                env_val = os.environ.get("TOTAL_BUDGET")
                if env_val is not None:
                    env_budget = float(env_val)
            except (ValueError, TypeError):
                pass
                
        import sqlite3
        with sqlite3.connect(self.db_path, timeout=30.0) as conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.cursor()
            cursor.execute("SELECT total_budget FROM ledger_metadata WHERE id = 1")
            row = cursor.fetchone()
            if row:
                if env_budget is not None and row[0] != env_budget:
                    cursor.execute("UPDATE ledger_metadata SET total_budget = ? WHERE id = 1", (env_budget,))
            else:
                initial_budget = env_budget if env_budget is not None else self.total_budget
                cursor.execute(
                    "INSERT INTO ledger_metadata (id, total_budget, reserve_floor, per_task_cap, total_spent) VALUES (1, ?, ?, ?, 0.0)",
                    (initial_budget, self.reserve_floor, self.per_task_cap)
                )
            conn.commit()

    def _load_pricing_config(self) -> Tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
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
                pass
        elif self.pricing_config:
            try:
                with open(self.pricing_config, "w") as f:
                    json.dump({"pricing": pricing, "tier_models": tier_models, "tier_thinking": tier_thinking, "custom_providers": custom_providers}, f, indent=4)
            except OSError:
                pass
        return _derive_cache_prices(pricing), tier_models, tier_thinking, custom_providers

    def get_unverified_models(self) -> List[str]:
        return [m for m, r in self.pricing.items() if not r.get("verified", False)]

    def _save_ledger(self, data: Dict[str, Any] = None):
        pass

    def get_remaining_budget(self) -> float:
        import sqlite3
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT total_budget, total_spent FROM ledger_metadata WHERE id = 1")
        row = cursor.fetchone()
        conn.close()
        if row:
            return max(0.0, row[0] - row[1])
        return 0.0

    def get_task_spend(self, task_id: str) -> float:
        import sqlite3
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT SUM(cost) FROM transactions WHERE task_id = ?", (task_id,))
        row = cursor.fetchone()
        conn.close()
        return row[0] if row[0] is not None else 0.0

    def calculate_cost(self, model: str, input_tokens: int, output_tokens: int, cache_write_tokens: int = 0, cache_read_tokens: int = 0) -> float:
        rates = self.pricing.get(model)
        if not rates:
            rates = max(self.pricing.values(), key=lambda r: r["input"])
        cost = (
            (input_tokens * rates["input"]) +
            (output_tokens * rates["output"]) +
            (cache_write_tokens * rates["cache_write"]) +
            (cache_read_tokens * rates["cache_read"])
        ) / 1000000.0
        return cost

    def record_spend(self, task_id: str, provider: str, model: str, input_tokens: int, output_tokens: int, cache_write_tokens: int = 0, cache_read_tokens: int = 0) -> float:
        cost = self.calculate_cost(model, input_tokens, output_tokens, cache_write_tokens, cache_read_tokens)
        import sqlite3
        with sqlite3.connect(self.db_path, timeout=30.0) as conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO transactions (task_id, provider, model, input_tokens, output_tokens, cache_write_tokens, cache_read_tokens, cost) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (task_id, provider, model, input_tokens, output_tokens, cache_write_tokens, cache_read_tokens, cost)
            )
            cursor.execute("UPDATE ledger_metadata SET total_spent = total_spent + ? WHERE id = 1", (cost,))
            conn.commit()
        return cost

    def record_state(self, task_id: str, state_name: str):
        import sqlite3
        with sqlite3.connect(self.db_path, timeout=30.0) as conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.cursor()
            cursor.execute("INSERT INTO loop_history (task_id, state_name) VALUES (?, ?)", (task_id, state_name))
            conn.commit()

    def check_runaway_loop(self, task_id: str) -> bool:
        import sqlite3
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT state_name FROM loop_history WHERE task_id = ? ORDER BY id ASC", (task_id,))
        rows = cursor.fetchall()
        conn.close()
        history = [r[0] for r in rows]
        if len(history) >= 4:
            last_four = history[-4:]
            if len(set(last_four)) == 1:
                return True
        return False

    def check_constraints(self, task_id: str, next_step_est_cost: float, is_thinker_escalation: bool, user_confirmed: bool = False) -> Tuple[bool, str]:
        import sqlite3
        import time
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT total_budget, total_spent, reserve_floor, per_task_cap FROM ledger_metadata WHERE id = 1")
        meta = cursor.fetchone()
        
        cutoff = time.time() - 600
        cursor.execute("SELECT SUM(est_cost) FROM reservations WHERE status = 'active' AND created_at > ?", (cutoff,))
        active_res = cursor.fetchone()[0] or 0.0
        
        cursor.execute("SELECT SUM(cost) FROM transactions WHERE task_id = ?", (task_id,))
        task_spent = cursor.fetchone()[0] or 0.0
        
        cursor.execute("SELECT SUM(est_cost) FROM reservations WHERE task_id = ? AND status = 'active' AND created_at > ?", (task_id, cutoff))
        task_active_res = cursor.fetchone()[0] or 0.0
        conn.close()
        
        if not meta:
            return False, "Ledger metadata missing"
        total_budget, total_spent, reserve_floor, per_task_cap = meta
        remaining_budget = max(0.0, total_budget - total_spent)
        available_budget = max(0.0, total_budget - (total_spent + active_res))
        
        if remaining_budget < 15.00 and is_thinker_escalation:
            if not user_confirmed:
                return False, f"ENDGAME: confirm required — est ${next_step_est_cost:.4f}, ${remaining_budget:.4f} left"
                
        if available_budget < next_step_est_cost:
            return False, f"Total budget exhausted. Required: ${next_step_est_cost:.4f}, Available: ${available_budget:.4f}."
            
        post_spend_budget = available_budget - next_step_est_cost
        if post_spend_budget < reserve_floor and not is_thinker_escalation:
            return False, f"Reserve floor threshold hit. Budget remaining: ${available_budget:.4f}. Only Thinker escalations allowed below ${reserve_floor:.2f}."
            
        if task_spent + task_active_res + next_step_est_cost > per_task_cap:
            return False, f"Per-task budget cap reached. Spent so far: ${task_spent:.4f}. Estimated next step: ${next_step_est_cost:.4f}. Task cap is ${per_task_cap:.2f}."
            
        return True, "Constraints satisfied"

    def reset_task_history(self, task_id: str):
        import sqlite3
        with sqlite3.connect(self.db_path, timeout=30.0) as conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.cursor()
            cursor.execute("DELETE FROM loop_history WHERE task_id = ?", (task_id,))
            conn.commit()

    def reserve(self, task_id: str, est_cost: float, is_thinker: bool = False, user_confirmed: bool = False) -> str:
        import sqlite3
        import time
        import uuid
        with sqlite3.connect(self.db_path, timeout=30.0) as conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.cursor()
            
            cursor.execute("SELECT total_budget, total_spent, reserve_floor, per_task_cap FROM ledger_metadata WHERE id = 1")
            meta = cursor.fetchone()
            if not meta:
                raise PermissionError("Ledger metadata missing")
            total_budget, total_spent, reserve_floor, per_task_cap = meta
            
            cutoff = time.time() - 600
            cursor.execute("SELECT SUM(est_cost) FROM reservations WHERE status = 'active' AND created_at > ?", (cutoff,))
            active_res = cursor.fetchone()[0] or 0.0
            
            cursor.execute("SELECT SUM(cost) FROM transactions WHERE task_id = ?", (task_id,))
            task_spent = cursor.fetchone()[0] or 0.0
            
            cursor.execute("SELECT SUM(est_cost) FROM reservations WHERE task_id = ? AND status = 'active' AND created_at > ?", (task_id, cutoff))
            task_active_res = cursor.fetchone()[0] or 0.0
            
            remaining_budget = max(0.0, total_budget - total_spent)
            available_budget = max(0.0, total_budget - (total_spent + active_res))
            
            if remaining_budget < 15.00 and is_thinker:
                if not user_confirmed:
                    raise PermissionError(f"ENDGAME: confirm required — est ${est_cost:.4f}, ${remaining_budget:.4f} left")
                    
            if available_budget < est_cost:
                raise PermissionError(f"Total budget exhausted. Required: ${est_cost:.4f}, Available: ${available_budget:.4f}.")
                
            post_spend_budget = available_budget - est_cost
            if post_spend_budget < reserve_floor and not is_thinker:
                raise PermissionError(f"Reserve floor threshold hit. Budget remaining: ${available_budget:.4f}. Only Thinker escalations allowed below ${reserve_floor:.2f}.")
                
            if task_spent + task_active_res + est_cost > per_task_cap:
                raise PermissionError(f"Per-task budget cap reached. Spent so far: ${task_spent:.4f}. Estimated next step: ${est_cost:.4f}. Task cap is ${per_task_cap:.2f}.")
                
            res_id = f"res-{uuid.uuid4().hex[:12]}"
            cursor.execute(
                "INSERT INTO reservations (id, task_id, est_cost, created_at, status) VALUES (?, ?, ?, ?, 'active')",
                (res_id, task_id, est_cost, time.time())
            )
            conn.commit()
            return res_id

    def commit(self, reservation_id: str, actual_cost: float, provider: str = None, model: str = None, input_tokens: int = 0, output_tokens: int = 0, cache_write_tokens: int = 0, cache_read_tokens: int = 0):
        import sqlite3
        with sqlite3.connect(self.db_path, timeout=30.0) as conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.cursor()
            
            cursor.execute("SELECT task_id, status FROM reservations WHERE id = ?", (reservation_id,))
            row = cursor.fetchone()
            if not row:
                raise ValueError(f"Reservation not found: {reservation_id}")
            task_id, status = row
            
            if status != "active":
                raise ValueError(f"Reservation is not active: {reservation_id} (status: {status})")
                
            cursor.execute("UPDATE reservations SET status = 'committed' WHERE id = ?", (reservation_id,))
            cursor.execute(
                "INSERT INTO transactions (task_id, provider, model, input_tokens, output_tokens, cache_write_tokens, cache_read_tokens, cost) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (task_id, provider, model, input_tokens, output_tokens, cache_write_tokens, cache_read_tokens, actual_cost)
            )
            cursor.execute("UPDATE ledger_metadata SET total_spent = total_spent + ? WHERE id = 1", (actual_cost,))
            conn.commit()

    def release(self, reservation_id: str):
        import sqlite3
        with sqlite3.connect(self.db_path, timeout=30.0) as conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.cursor()
            cursor.execute("UPDATE reservations SET status = 'released' WHERE id = ?", (reservation_id,))
            conn.commit()
