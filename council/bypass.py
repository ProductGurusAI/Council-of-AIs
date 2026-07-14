import re
import httpx
import os
from typing import Tuple, List, Optional
from dotenv import load_dotenv

load_dotenv()

# Common patterns for simple, trivial one-shot requests
SIMPLE_PATTERNS = [
    r"^(hi|hello|hey|greetings|yo)(\s+.*)?$",
    r"^(help|what can you do|explain yourself)(\s+.*)?$",
    r"^(write a joke|tell me a joke)$",
    r"^what is the time(\s+.*)?$",
    r"^what is \d+[\+\-\*\/]\d+\??$", # basic calculator questions
    r"^(thank you|thanks|bye|goodbye)$"
]

class BypassLane:
    def __init__(self, ollama_url: str = None, anthropic_api_key: str = None):
        self.ollama_url = ollama_url or os.getenv("OLLAMA_API_URL", "http://localhost:11434")
        self.anthropic_key = anthropic_api_key or os.getenv("ANTHROPIC_API_KEY")

    def classify(self, prompt: str, touch_list: Optional[List[str]] = None) -> Tuple[bool, str, float]:
        """
        Classifies whether a user prompt should bypass the main Council gates.
        Returns: (should_bypass, tier_used, cost)
        """
        touch_list = touch_list or []
        prompt_stripped = prompt.strip().lower()

        # --- Tier 1: Regex & Structural Heuristics ---
        # 1. Check regex matches for simple questions
        for pattern in SIMPLE_PATTERNS:
            if re.match(pattern, prompt_stripped):
                return True, "Tier 1 (Regex)", 0.0

        # 2. Check structural invariants: if short, no question mark, and no project keywords
        project_keywords = ["schema", "auth", "interface", "database", "git", "config", "refactor", "build", "api"]
        has_touch_list_match = any(item.lower() in prompt_stripped for item in touch_list)
        has_keyword_match = any(kw in prompt_stripped for kw in project_keywords)

        if len(prompt) < 40 and not has_touch_list_match and not has_keyword_match:
            return True, "Tier 1 (Structural)", 0.0

        # If it clearly touches project context, never bypass
        if has_touch_list_match or "refactor" in prompt_stripped or "implement" in prompt_stripped:
            return False, "Tier 1 (Project Block)", 0.0

        # --- Tier 2: Local Small Model (Ollama) ---
        classification_prompt = (
            "Classify this user request. Answer ONLY with 'BYPASS' if the request is a simple, "
            "trivial, one-shot question that requires no project context, code structure, deep reasoning, "
            "or architectural planning. Otherwise, answer 'GATES'.\n"
            f"Request: {prompt}\n"
            "Classification:"
        )

        try:
            # We call the Ollama API with a low timeout to preserve speed
            with httpx.Client(timeout=1.5) as client:
                # We request a fast model like 'llama3', 'phi3', or 'gemma:2b'
                # If we don't know what models are installed, we can query /api/tags
                # or just attempt to generate using a default 'phi3' or 'llama3'.
                # Let's try 'phi3' first as a fast small model, fallback to any available tags.
                model = "phi3"
                
                # Check active models
                try:
                    tags_resp = client.get(f"{self.ollama_url}/api/tags")
                    if tags_resp.status_code == 200:
                        models = [m["name"] for m in tags_resp.json().get("models", [])]
                        if models:
                            # Use whatever model is installed, preference for small ones
                            for preferred in ["phi3", "llama3", "gemma"]:
                                match = [m for m in models if preferred in m]
                                if match:
                                    model = match[0]
                                    break
                            else:
                                model = models[0]
                except Exception:
                    pass # Keep using default 'phi3'

                payload = {
                    "model": model,
                    "prompt": classification_prompt,
                    "stream": False,
                    "options": {
                        "temperature": 0.0,
                        "num_predict": 5
                    }
                }
                resp = client.post(f"{self.ollama_url}/api/generate", json=payload)
                if resp.status_code == 200:
                    result = resp.json().get("response", "").strip().upper()
                    # Safe fall-through: must be exactly "BYPASS" to bypass, uncertainty fails safe.
                    if "BYPASS" in result and "GATES" not in result:
                        return True, f"Tier 2 (Ollama: {model})", 0.0
                    else:
                        return False, f"Tier 2 (Ollama: {model})", 0.0
        except Exception:
            # Ollama offline/slow, OR httpx client construction failed entirely
            # (proxy env, missing extras). Tier 2 must NEVER take down routing —
            # any failure here falls through to Tier 3 / the safe default.
            pass

        # --- Tier 3: Cheap API Fallback (Claude Haiku) ---
        if self.anthropic_key:
            try:
                # Direct mini-call to Haiku.
                # Cost is input (~50 tokens) + output (~5 tokens) on Haiku class model.
                import anthropic
                client = anthropic.Anthropic(api_key=self.anthropic_key)
                
                # Setup classification prompt
                response = client.messages.create(
                    model="claude-3-haiku-20240307",
                    max_tokens=5,
                    temperature=0.0,
                    system="You are a classifier. Output ONLY 'BYPASS' for simple conversational trivia, and 'GATES' for everything else.",
                    messages=[
                        {"role": "user", "content": f"Classify this request: '{prompt}'"}
                    ]
                )
                
                result = response.content[0].text.strip().upper()
                
                # Ledger estimation for classification cost
                input_tokens = len(prompt) // 4 + 40
                output_tokens = response.usage.output_tokens
                # Haiku rates: $0.25/1M input, $1.25/1M output
                cost = ((input_tokens * 0.25) + (output_tokens * 1.25)) / 1000000.0
                
                if "BYPASS" in result and "GATES" not in result:
                    return True, "Tier 3 (Haiku API)", cost
                else:
                    return False, "Tier 3 (Haiku API)", cost
            except Exception:
                pass

        # Fall-safe: if all else fails, do not bypass (expensive-safe path)
        return False, "Fallback Default (Gates)", 0.0
