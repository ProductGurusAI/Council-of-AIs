import os
import time
from typing import Dict, Any, List, Optional, Tuple
from dotenv import load_dotenv
from council.ledger import Ledger

load_dotenv()

class CacheEnvelope:
    """
    Exposes the Layered Cache Envelope contract:
    Layer 1: System prompt + Invariants (stable prefix)
    Layer 2: Decisions + Open Questions
    Layer 3: Task instructions + Mermaid Canvas
    Layer 4: Volatile turn content (user/assistant history)
    """
    def __init__(self, system_and_invariants: str, decisions_and_questions: str, task_and_canvas: str):
        self.layer1 = system_and_invariants.strip()
        self.layer2 = decisions_and_questions.strip()
        self.layer3 = task_and_canvas.strip()
        self.layer4_turns: List[Dict[str, str]] = [] # list of {"role": "user/assistant", "content": "..."}

    def add_turn(self, role: str, content: str):
        self.layer4_turns.append({"role": role, "content": content.strip()})

    def estimate_tokens(self) -> Dict[str, int]:
        """Estimate tokens for each layer using character-count heuristic (chars // 4)."""
        return {
            "layer1": max(1, len(self.layer1) // 4),
            "layer2": max(1, len(self.layer2) // 4),
            "layer3": max(1, len(self.layer3) // 4),
            "layer4": sum(max(1, len(turn["content"]) // 4) for turn in self.layer4_turns)
        }

class UnifiedClient:
    def __init__(self, ledger: Ledger):
        self.ledger = ledger
        self.anthropic_key = os.getenv("ANTHROPIC_API_KEY")
        self.openai_key = os.getenv("OPENAI_API_KEY")
        self.gemini_key = os.getenv("GEMINI_API_KEY")
        
        # Force simulation in unit tests or if mock keys are used
        import sys
        is_unittest = "unittest" in sys.modules or any("unittest" in arg for arg in sys.argv)
        is_mock = lambda k: not k or "testkey" in k.lower() or "mock" in k.lower()
        self.is_simulated = is_unittest or \
                            not (self.anthropic_key or self.openai_key or self.gemini_key) or \
                            (is_mock(self.anthropic_key) and is_mock(self.openai_key) and is_mock(self.gemini_key))

    def select_model(self, tier: str, provider: str = "anthropic") -> str:
        """Select model name from the Ledger's config-driven tier map (models.json)."""
        slot = tier if tier in ("thinker", "explorer") else "cheap"
        # 1. Flat config support: only honor a NON-EMPTY string. The dashboard
        # saves {"thinker": "", ...} before the user picks a mapping —
        # isinstance("", str) is True, so an unguarded check returns "" and the
        # pricing validation downstream hard-fails every call.
        flat_val = self.ledger.tier_models.get(slot)
        if isinstance(flat_val, str) and flat_val.strip():
            return flat_val.strip()

        # 2. Nested/Provider config support
        provider_map = self.ledger.tier_models.get(provider, {})
        model = provider_map.get(slot) if isinstance(provider_map, dict) else None
        if isinstance(model, str) and model.strip():
            return model.strip()

        # 3. Any provider's nested mapping for this slot
        for val in self.ledger.tier_models.values():
            if isinstance(val, dict):
                candidate = val.get(slot)
                if isinstance(candidate, str) and candidate.strip():
                    return candidate.strip()

        # 4. Last resort: hardcoded defaults per slot (priced in default config)
        defaults = {"thinker": "claude-opus-4-8", "explorer": "claude-sonnet-5", "cheap": "claude-haiku-4-5-20251001"}
        return defaults[slot]

    def execute_task(
        self,
        task_id: str,
        tier: str,
        provider: str,
        cache_envelope: CacheEnvelope,
        user_prompt: str,
        warm_cache: bool = True,
        user_confirmed: bool = False
    ) -> Tuple[str, float]:
        """
        Executes a prompt against a provider under the budget controls.
        Determines the caching economics and records the transactions.
        
        Returns: (model_response, cost)
        """
        model = self.select_model(tier, provider)
        if model not in self.ledger.pricing:
            raise ValueError(f"Model pricing configuration missing for: {model}")
        provider = self.ledger.pricing[model]["provider"]
        
        # 1. Estimate caching economics
        token_map = cache_envelope.estimate_tokens()
        prompt_tokens = len(user_prompt) // 4
        
        # Prefix length (Layers 1 + 2 + 3)
        prefix_tokens = token_map["layer1"] + token_map["layer2"] + token_map["layer3"]
        volatile_tokens = token_map["layer4"] + prompt_tokens

        # Billing breakdown based on cache state
        input_tokens = 0
        output_tokens = 0
        cache_write_tokens = 0
        cache_read_tokens = 0

        # Anthropic caching model:
        # Cache writes cost a 25% premium on input, reads cost 80% discount
        if provider == "anthropic":
            if not warm_cache:
                # Cold cache: we pay input for everything, plus cache write cost for prefix
                input_tokens = volatile_tokens
                cache_write_tokens = prefix_tokens
            else:
                # Warm cache: prefix is read, we only pay input for volatile turns + prompt
                input_tokens = volatile_tokens
                cache_read_tokens = prefix_tokens
        
        # OpenAI caching model:
        # Prefix caching is automatic. If cache is warm, the prefix is 50% discount. No write premium.
        elif provider == "openai":
            if not warm_cache:
                input_tokens = prefix_tokens + volatile_tokens
            else:
                input_tokens = volatile_tokens
                cache_read_tokens = prefix_tokens
                
        # Google/Gemini and other fallbacks
        else:
            if not warm_cache:
                input_tokens = prefix_tokens + volatile_tokens
            else:
                input_tokens = volatile_tokens
                cache_read_tokens = prefix_tokens

        # Estimated outputs: we mock output length based on tier
        if tier == "thinker":
            est_output_tokens = 800
        elif tier == "explorer":
            est_output_tokens = 400
        else:
            est_output_tokens = 150

        est_cost = self.ledger.calculate_cost(model, input_tokens, est_output_tokens, cache_write_tokens, cache_read_tokens)

        # Verify ledger constraints (pre-flight check uses the estimate; the
        # recorded spend below uses ACTUAL usage returned by the provider — FR-5)
        is_thinker = (tier == "thinker")
        allowed, reason = self.ledger.check_constraints(task_id, est_cost, is_thinker, user_confirmed=user_confirmed)
        if not allowed:
            raise PermissionError(f"Ledger block: {reason}")

        # 2. Run API invocation (Real or Simulated)
        response_text = ""
        usage = None  # actual provider-reported usage dict

        if self.is_simulated:
            time.sleep(0.5) # simulate latency
            response_text = self._generate_simulated_response(tier, user_prompt)
            # Simulation: estimates are the only numbers we have
            usage = {
                "input_tokens": input_tokens,
                "output_tokens": len(response_text) // 4,
                "cache_write_tokens": cache_write_tokens,
                "cache_read_tokens": cache_read_tokens,
            }
        else:
            try:
                response_text, usage = self._call_real_api(
                    provider, model, tier, cache_envelope, user_prompt, prefix_tokens
                )
            except Exception as e:
                # API failure: return a clearly-labeled simulated response and
                # record ZERO spend — a failed call must never bill the budget.
                response_text = (
                    f"[SIMULATED FALLBACK — API ERROR, NO SPEND RECORDED: {str(e)}]\n"
                    + self._generate_simulated_response(tier, user_prompt)
                )
                usage = None

        # 3. Record spend from actual usage (or estimates in simulation).
        if usage is not None:
            cost = self.ledger.record_spend(
                task_id=task_id,
                provider=provider,
                model=model,
                input_tokens=usage.get("input_tokens", 0),
                output_tokens=usage.get("output_tokens", 0),
                cache_write_tokens=usage.get("cache_write_tokens", 0),
                cache_read_tokens=usage.get("cache_read_tokens", 0)
            )
        else:
            cost = 0.0

        # Add to volatile turns
        cache_envelope.add_turn("user", user_prompt)
        cache_envelope.add_turn("assistant", response_text)

        return response_text, cost

    def _call_real_api(self, provider: str, model: str, tier: str, cache_envelope: CacheEnvelope, user_prompt: str, prefix_tokens: int) -> Tuple[str, Dict[str, int]]:
        """
        Calls the actual provider API.
        Returns (response_text, usage) where usage carries the PROVIDER-REPORTED
        token counts (including cache read/write breakdowns) — never estimates.
        """
        if provider == "anthropic":
            import anthropic
            client = anthropic.Anthropic(api_key=self.anthropic_key)

            # Construct caching parameters. Anthropic rejects cache_control on
            # EMPTY text blocks with a 400 ("cache_control cannot be set for
            # empty text blocks") — collaboration/quiz envelopes legitimately
            # leave layers blank, so only include non-empty layers.
            system_blocks = [
                {"type": "text", "text": layer, "cache_control": {"type": "ephemeral"}}
                for layer in (cache_envelope.layer1, cache_envelope.layer2, cache_envelope.layer3)
                if layer and layer.strip()
            ]
            if not system_blocks:
                system_blocks = [{"type": "text", "text": "Council of AIs task."}]

            messages = []
            for turn in cache_envelope.layer4_turns:
                messages.append({"role": turn["role"], "content": turn["content"]})
            messages.append({"role": "user", "content": user_prompt})

            max_toks = 4000 if tier == "thinker" else 1500
            kwargs = {
                "model": model,
                "max_tokens": max_toks,
                "system": system_blocks,
                "messages": messages
            }

            # Support reasoning/thinking parameters
            thinking_level = self.ledger.tier_thinking.get(tier, "low" if tier == "explorer" else "medium")
            rates = self.ledger.pricing.get(model, {})
            if thinking_level in ("low", "medium", "high") and rates.get("supports_thinking") is True:
                budget_map = {"low": 1024, "medium": 2048, "high": 4096}
                budget = budget_map[thinking_level]
                kwargs["thinking"] = {"type": "enabled", "budget_tokens": budget}
                kwargs["max_tokens"] = budget + 2048

            response = client.messages.create(**kwargs)

            # With extended thinking enabled the response contains thinking
            # blocks BEFORE text blocks — content[0] is not guaranteed to be
            # text. Join all text-type blocks; never assume block order.
            text = "\n".join(
                block.text for block in response.content
                if getattr(block, "type", "") == "text" and hasattr(block, "text")
            )
            if not text:
                text = "[No text content in response]"
            u = response.usage
            usage = {
                "input_tokens": getattr(u, "input_tokens", 0),
                "output_tokens": getattr(u, "output_tokens", 0),
                "cache_write_tokens": getattr(u, "cache_creation_input_tokens", 0) or 0,
                "cache_read_tokens": getattr(u, "cache_read_input_tokens", 0) or 0,
            }
            return text, usage

        elif provider == "openai":
            from openai import OpenAI
            client = OpenAI(api_key=self.openai_key)

            # Combine System, Decisions, and Task as system message to utilize automatic prefix caching
            system_msg = f"{cache_envelope.layer1}\n\n=== DECISIONS & QUESTIONS ===\n{cache_envelope.layer2}\n\n=== TASK & CANVAS ===\n{cache_envelope.layer3}"

            messages = [{"role": "system", "content": system_msg}]
            for turn in cache_envelope.layer4_turns:
                messages.append({"role": turn["role"], "content": turn["content"]})
            messages.append({"role": "user", "content": user_prompt})

            max_toks = 2000 if tier == "thinker" else 800
            kwargs = {
                "model": model,
                "messages": messages,
                "max_tokens": max_toks
            }

            # Support reasoning/thinking parameters for o1/o3/sol models
            if model.lower().startswith(("o1", "o3")) or "sol" in model.lower():
                kwargs["max_completion_tokens"] = kwargs.pop("max_tokens")
                thinking_level = self.ledger.tier_thinking.get(tier, "low" if tier == "explorer" else "medium")
                if thinking_level in ("low", "medium", "high"):
                    kwargs["reasoning_effort"] = thinking_level

            response = client.chat.completions.create(**kwargs)

            text = response.choices[0].message.content or ""
            usage = {"input_tokens": 0, "output_tokens": len(text) // 4,
                     "cache_write_tokens": 0, "cache_read_tokens": 0}
            if response.usage:
                prompt_tokens = response.usage.prompt_tokens or 0
                cached = 0
                details = getattr(response.usage, "prompt_tokens_details", None)
                if details is not None:
                    cached = getattr(details, "cached_tokens", 0) or 0
                usage["input_tokens"] = max(0, prompt_tokens - cached)
                usage["cache_read_tokens"] = cached
                usage["output_tokens"] = response.usage.completion_tokens or 0
            return text, usage

        elif provider == "gemini":
            import google.generativeai as genai
            genai.configure(api_key=self.gemini_key)

            # For Gemini we concatenate the context in system instructions
            system_instruction = f"{cache_envelope.layer1}\n\n{cache_envelope.layer2}\n\n{cache_envelope.layer3}"

            model_client = genai.GenerativeModel(
                model_name=model,
                system_instruction=system_instruction
            )

            # Build history without issuing intermediate generate calls
            # (the old send_message-per-turn loop re-billed every prior turn).
            history = []
            for turn in cache_envelope.layer4_turns:
                role = "model" if turn["role"] == "assistant" else "user"
                history.append({"role": role, "parts": [turn["content"]]})

            chat = model_client.start_chat(history=history)
            response = chat.send_message(user_prompt)
            text = response.text
            usage = {"input_tokens": 0, "output_tokens": len(text) // 4,
                     "cache_write_tokens": 0, "cache_read_tokens": 0}
            meta = getattr(response, "usage_metadata", None)
            if meta is not None:
                prompt_toks = getattr(meta, "prompt_token_count", 0) or 0
                cached = getattr(meta, "cached_content_token_count", 0) or 0
                usage["input_tokens"] = max(0, prompt_toks - cached)
                usage["cache_read_tokens"] = cached
                usage["output_tokens"] = getattr(meta, "candidates_token_count", 0) or (len(text) // 4)
            return text, usage

        elif provider in getattr(self.ledger, "custom_providers", {}):
            provider_cfg = self.ledger.custom_providers[provider]
            base_url = provider_cfg.get("base_url")
            key_env_var = provider_cfg.get("key_env_var")

            # Retrieve API key
            api_key = os.getenv(key_env_var) if key_env_var else None
            if not api_key:
                raise ValueError(f"API key environment variable '{key_env_var}' is not set for custom provider '{provider}'")

            from openai import OpenAI
            client = OpenAI(api_key=api_key, base_url=base_url)

            system_msg = f"{cache_envelope.layer1}\n\n=== DECISIONS & QUESTIONS ===\n{cache_envelope.layer2}\n\n=== TASK & CANVAS ===\n{cache_envelope.layer3}"
            messages = [{"role": "system", "content": system_msg}]
            for turn in cache_envelope.layer4_turns:
                messages.append({"role": turn["role"], "content": turn["content"]})
            messages.append({"role": "user", "content": user_prompt})

            max_toks = 2000 if tier == "thinker" else 800
            kwargs = {
                "model": model,
                "messages": messages,
                "max_tokens": max_toks
            }

            response = client.chat.completions.create(**kwargs)
            text = response.choices[0].message.content or ""
            usage = {"input_tokens": 0, "output_tokens": len(text) // 4,
                     "cache_write_tokens": 0, "cache_read_tokens": 0}
            if response.usage:
                usage["input_tokens"] = response.usage.prompt_tokens or 0
                usage["output_tokens"] = response.usage.completion_tokens or 0
            return text, usage

        raise ValueError(f"Unknown provider: {provider}")

    def _generate_simulated_response(self, tier: str, prompt: str) -> str:
        """Helper to create realistic mocked text for simulation mode."""
        if tier == "thinker":
            return (
                f"[Thinker Response (Opus-tier)]\n"
                f"Parsed architecture constraints and reviewed schema invariants.\n"
                f"Proposed Solution: Design requires decoupling the API request handlers from the state loop.\n"
                f"Let's write a checklist for validation:\n"
                f"1. Database commits must be atomic.\n"
                f"2. Error handler catches JSONDecodeErrors and quarantines entries.\n"
                f"3. Runaway counter triggers warning.\n"
                f"Verification checklist created."
            )
        elif tier == "explorer":
            return (
                f"[Explorer Response (Sonnet/Flash-tier)]\n"
                f"Executing drafting task for: '{prompt}'.\n"
                f"Drafted components. Everything works locally. File created under refs/task_draft.py."
            )
        else:
            return f"[Leader Response (Haiku-tier)] Trivial input processed. Route confirmed."
