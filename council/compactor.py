from typing import Tuple, List, Dict
import re
from council.client import CacheEnvelope

class ContextCompactor:
    def __init__(self, max_capacity_tokens: int = 8000, model_name: str = "claude-3-5-sonnet-20240620"):
        self.max_capacity_tokens = max_capacity_tokens
        self.model_name = model_name

    def check_limits(self, envelope: CacheEnvelope) -> str:
        """
        Analyzes the token usage of the CacheEnvelope.
        Returns: 'aggressive' if > 85%, 'mild' if > 50%, or 'normal'.
        """
        tokens_dict = envelope.estimate_tokens()
        total_tokens = sum(tokens_dict.values())
        
        usage_pct = total_tokens / self.max_capacity_tokens
        if usage_pct >= 0.85:
            return "aggressive"
        elif usage_pct >= 0.50:
            return "mild"
        return "normal"

    def compact_mild(self, envelope: CacheEnvelope):
        """
        Mild Compaction (50% trigger):
        Drops middle turns from Layer 4 (volatile history), preserving the prefix
        and only keeping the initial turn and the latest 2 turns to keep the cache warm.

        NOTE: no "system"-role turns are ever inserted — provider message APIs
        (Anthropic in particular) reject system roles inside the message list.
        The strip marker is merged into the first retained tail turn instead,
        and consecutive same-role turns are merged to preserve user/assistant
        alternation.
        """
        turns = envelope.layer4_turns
        if len(turns) <= 3:
            return # Too few turns to drop anything safely

        # Keep the first turn (initial instruction) and the last two turns
        first_turn = dict(turns[0])
        latest_turns = [dict(t) for t in turns[-2:]]

        # Merge the strip marker into the first retained tail turn
        marker = "[... Stale intermediate context stripped to preserve cache ...]"
        latest_turns[0]["content"] = f"{marker}\n{latest_turns[0]['content']}"

        # Rebuild, merging any consecutive same-role turns to keep alternation valid
        compacted_turns = [first_turn]
        for turn in latest_turns:
            if compacted_turns[-1]["role"] == turn["role"]:
                compacted_turns[-1]["content"] += "\n" + turn["content"]
            else:
                compacted_turns.append(turn)

        envelope.layer4_turns = compacted_turns

    def compact_aggressive(self, envelope: CacheEnvelope, cost_calculator, remaining_turns_est: int = 5) -> Tuple[bool, str]:
        """
        Aggressive Compaction (85% trigger):
        Evaluates the economic trade-off of resetting the prefix cache.
        If rebuilding the prefix cache costs less than the savings achieved by stripping
        all intermediate trace/tool logs for the remaining turns, it strips verbose logs
        and returns True (initiating a cache epoch reset).
        
        Hard-blocked from touching:
        - Layer 1 Invariants
        - Layer 2 Decisions
        - Active task instruction
        """
        tokens_dict = envelope.estimate_tokens()
        prefix_tokens = tokens_dict["layer1"] + tokens_dict["layer2"] + tokens_dict["layer3"]
        
        # Estimate size of verbose tool logs/traces in Layer 4
        verbose_tokens = 0
        cleaned_turns = []
        
        for turn in envelope.layer4_turns:
            content = turn["content"]
            # Detect tool blocks, raw HTML, or trace lines
            # Example: [TOOL LOG] or ```json ... [long response]
            cleaned_content = re.sub(
                r"(```json\s*\n\{\s*\"tool_output\".*?\n```|\[TOOL LOG\].*?\n|\[DEBUG\].*?\n)",
                "[... Raw logs stripped ...]\n",
                content,
                flags=re.DOTALL
            )
            verbose_tokens += (len(content) - len(cleaned_content)) // 4
            cleaned_turns.append({"role": turn["role"], "content": cleaned_content})

        if verbose_tokens <= 0:
            return False, "No verbose logs to strip."

        # Compute cost of cache reset:
        # Rebuilding requires processing prefix_tokens at cache_write premium (Anthropic) or input rates (OpenAI).
        # We calculate cost of writing prefix + reading it for the remaining_turns_est.
        # Savings = verbose_tokens * input cost * remaining_turns_est.
        
        cost_rebuild = cost_calculator(
            model=self.model_name,
            input_tokens=0,
            output_tokens=0,
            cache_write_tokens=prefix_tokens,
            cache_read_tokens=0
        )
        
        cost_saved_per_turn = cost_calculator(
            model=self.model_name,
            input_tokens=verbose_tokens,
            output_tokens=0
        )
        total_projected_savings = cost_saved_per_turn * remaining_turns_est

        # Epoch reset decision
        if total_projected_savings > cost_rebuild:
            envelope.layer4_turns = cleaned_turns
            return True, f"Aggressive compaction applied: reset cache epoch. Cost to rebuild: ${cost_rebuild:.5f}, Projected savings: ${total_projected_savings:.5f}."
            
        return False, f"Aggressive compaction skipped: rebuild cost (${cost_rebuild:.5f}) exceeds projected savings (${total_projected_savings:.5f})."
