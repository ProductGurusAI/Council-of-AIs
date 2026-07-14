import os
import re
import json
import sqlite3
import subprocess
from typing import Dict, Any, List, Optional, Tuple

try:
    import yaml as _yaml
except ImportError:
    _yaml = None  # graceful fallback to the naive line parser

# FR-25 is enforced by TIER, not by a hardcoded model-name list — a name list
# silently stops banning anything the moment a new cheap model ships.
# `author_tier` in front-matter is authoritative; the name patterns below are
# only a fallback inference for entries that predate the tier field.
BANNED_AUTHOR_TIERS = ("cheap", "leader", "bypass")
CHEAP_MODEL_NAME_PATTERNS = ("haiku", "mini", "flash", "nano", "lite", "phi", "gemma")

# Legacy explicit list kept for backward compatibility with older entries.
CHEAP_MODELS = [
    "claude-3-haiku-20240307",
    "claude-haiku-4-5-20251001",
    "gpt-4o-mini",
    "gemini-3.5-flash"
]


def _infer_is_cheap(author_model: str) -> bool:
    if author_model in CHEAP_MODELS:
        return True
    lowered = author_model.lower()
    return any(p in lowered for p in CHEAP_MODEL_NAME_PATTERNS)

class MemoryWrapper:
    def __init__(self, workspace_path: str = "."):
        self.workspace_path = workspace_path
        self.council_dir = os.path.join(workspace_path, ".council")
        self.memory_dir = os.path.join(self.council_dir, "memory")
        self.quarantine_dir = os.path.join(self.council_dir, "quarantine")
        self.db_path = os.path.join(self.memory_dir, "transcripts.db")

        # Create directories
        os.makedirs(self.memory_dir, exist_ok=True)
        os.makedirs(self.quarantine_dir, exist_ok=True)

        self._init_git()
        self._init_db()

    def _init_git(self):
        """Initializes a git repository inside the workspace if not already present."""
        # Initialize git in the workspace path for simplicity
        if not os.path.exists(os.path.join(self.workspace_path, ".git")):
            try:
                subprocess.run(["git", "init"], cwd=self.workspace_path, capture_output=True, check=True)
                # Create a gitignore to not commit transcripts.db if desired, or database files
                gitignore_path = os.path.join(self.workspace_path, ".gitignore")
                if not os.path.exists(gitignore_path):
                    with open(gitignore_path, "w") as f:
                        f.write(".council/memory/transcripts.db\n")
            except Exception:
                pass # Git command not available or failed

    def _init_db(self):
        """Initializes the SQLite database for L0 transcripts and embeddings."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS transcripts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL,
                turn_index INTEGER NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                tokens INTEGER,
                cost REAL,
                model TEXT,
                tier TEXT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # Migrate pre-existing DBs that lack the audit columns
        for col in ("model", "tier"):
            try:
                cursor.execute(f"ALTER TABLE transcripts ADD COLUMN {col} TEXT")
            except sqlite3.OperationalError:
                pass  # column already exists
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS embeddings (
                transcript_id INTEGER PRIMARY KEY,
                embedding TEXT NOT NULL, -- JSON serialized float list
                FOREIGN KEY (transcript_id) REFERENCES transcripts (id)
            )
        """)
        conn.commit()
        conn.close()

    def parse_markdown_with_yaml(self, content: str) -> Tuple[Dict[str, Any], str]:
        """
        Parses YAML front-matter from a markdown string.
        Returns: (metadata_dict, markdown_body)
        """
        # Match YAML block starting at the top of the file
        match = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)$", content, re.DOTALL)
        if not match:
            return {}, content

        yaml_text = match.group(1)
        body = match.group(2)

        # Prefer real YAML parsing (nested fields like reopen_condition lists
        # survive); fall back to flat key:value only if PyYAML is unavailable.
        if _yaml is not None:
            try:
                parsed = _yaml.safe_load(yaml_text)
                if isinstance(parsed, dict):
                    return parsed, body
            except _yaml.YAMLError:
                return {}, content  # malformed front-matter -> caller quarantines

        metadata = {}
        for line in yaml_text.split("\n"):
            line = line.strip()
            if not line or ":" not in line:
                continue
            k, v = line.split(":", 1)
            # Clean quotes and strip whitespace
            v_cleaned = v.strip().strip("'").strip('"')
            metadata[k.strip()] = v_cleaned

        return metadata, body

    def serialize_with_yaml(self, metadata: Dict[str, Any], body: str) -> str:
        """Serializes metadata and body back to Markdown with YAML front-matter."""
        if _yaml is not None:
            yaml_text = _yaml.safe_dump(metadata, default_flow_style=False, sort_keys=False).strip()
            return f"---\n{yaml_text}\n---\n{body}"
        yaml_lines = ["---"]
        for k, v in metadata.items():
            yaml_lines.append(f"{k}: {v}")
        yaml_lines.append("---")
        return "\n".join(yaml_lines) + "\n" + body

    def _check_lock(self):
        lock_file = os.path.join(self.workspace_path, ".council", "memory", "write_lockout.lock")
        if os.path.exists(lock_file):
            try:
                with open(lock_file, "r") as lf:
                    reason = lf.read().strip()
            except Exception:
                reason = "Unknown write lockout trigger."
            raise PermissionError(f"Workspace write lockout active. Memory writes are rejected. Reason: {reason}")

    def validate_and_save_entry(self, filename: str, content: str) -> bool:
        """
        Validates front-matter and writes the entry to the memory directory.
        Quarantines the file if parsing fails.
        """
        self._check_lock()
        filepath = os.path.join(self.memory_dir, filename)
        metadata, body = self.parse_markdown_with_yaml(content)

        # Basic validations
        required_fields = ["class", "author_model", "task_id"]
        is_valid = True
        for field in required_fields:
            if field not in metadata or not metadata[field]:
                is_valid = False
                break

        # §7.3: reopen_condition is MANDATORY on Decisions — it is what lets a
        # future model distinguish "settled" from "settled given an assumption
        # that just changed". A Decision without one is an invalid entry.
        if is_valid and metadata.get("class") == "decision" and not metadata.get("reopen_condition"):
            is_valid = False

        # FR-25: Authorship Rule check — tier-based, with name-pattern fallback
        if is_valid:
            author = str(metadata["author_model"])
            author_tier = str(metadata.get("author_tier", "")).lower()
            entry_class = metadata["class"]

            is_banned_author = (
                author_tier in BANNED_AUTHOR_TIERS
                if author_tier
                else _infer_is_cheap(author)
            )
            if is_banned_author and entry_class in ["invariant", "decision"]:
                raise PermissionError(
                    f"FR-25 Authorship Violation: Cheap-tier model '{author}' is banned from "
                    f"authoring Class '{entry_class}' entries."
                )

        if not is_valid:
            # Quarantine the entry
            quarantine_path = os.path.join(self.quarantine_dir, filename)
            with open(quarantine_path, "w") as f:
                f.write(content)
            raise ValueError(f"YAML front-matter validation failed. Entry quarantined to: {quarantine_path}")

        # Save to memory directory
        with open(filepath, "w") as f:
            f.write(content)
        
        return True

    def append_transcript(self, task_id: str, turn_index: int, role: str, content: str, tokens: int = 0, cost: float = 0.0, embedding: Optional[List[float]] = None, model: Optional[str] = None, tier: Optional[str] = None) -> int:
        """Saves a raw turn transcript (L0) and its optional embedding to SQLite."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO transcripts (task_id, turn_index, role, content, tokens, cost, model, tier) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (task_id, turn_index, role, content, tokens, cost, model, tier)
        )
        row_id = cursor.lastrowid
        
        if embedding and row_id:
            cursor.execute(
                "INSERT INTO embeddings (transcript_id, embedding) VALUES (?, ?)",
                (row_id, json.dumps(embedding))
            )
            
        conn.commit()
        conn.close()
        return row_id or 0

    def query_similar_transcripts(self, query_embedding: List[float], limit: int = 5) -> List[Tuple[int, str, str, float]]:
        """
        Pure-Python Cosine Similarity search fallback over transcripts.
        Returns list of (transcript_id, task_id, content, score) sorted by similarity score.
        """
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT t.id, t.task_id, t.content, e.embedding FROM transcripts t JOIN embeddings e ON t.id = e.transcript_id")
        rows = cursor.fetchall()
        conn.close()

        results = []
        for row_id, task_id, content, emb_json in rows:
            try:
                emb = json.loads(emb_json)
                # Compute Cosine Similarity
                dot_product = sum(x*y for x, y in zip(query_embedding, emb))
                norm_v1 = sum(x*x for x in query_embedding) ** 0.5
                norm_v2 = sum(y*y for y in emb) ** 0.5
                if norm_v1 == 0 or norm_v2 == 0:
                    similarity = 0.0
                else:
                    similarity = dot_product / (norm_v1 * norm_v2)
                results.append((row_id, task_id, content, similarity))
            except Exception:
                continue

        # Sort by similarity score descending
        results.sort(key=lambda x: x[3], reverse=True)
        return results[:limit]

    def commit_task_boundary(self, task_id: str) -> bool:
        """
        Commits memory updates to local Git and tags with the task ID.
        Returns True if the boundary was recorded (committed, or nothing to
        commit), False if the audit trail FAILED to record — callers must
        surface that, never swallow it (FR-24).
        """
        self._check_lock()
        try:
            # Check for changes in memory dir
            subprocess.run(["git", "add", ".council/memory/"], cwd=self.workspace_path, check=True, capture_output=True)

            # Run commit
            commit_res = subprocess.run(
                ["git", "commit", "-m", f"Task Merge: {task_id}"],
                cwd=self.workspace_path, capture_output=True, text=True
            )

            if commit_res.returncode == 0:
                # Delete existing tag if it matches (for overwrite updates)
                subprocess.run(["git", "tag", "-d", task_id], cwd=self.workspace_path, capture_output=True)
                subprocess.run(
                    ["git", "tag", "-a", task_id, "-m", f"Task Boundary for {task_id}"],
                    cwd=self.workspace_path, check=True, capture_output=True
                )
                return True

            # "nothing to commit" is a legitimate no-op boundary, not a failure
            combined = (commit_res.stdout or "") + (commit_res.stderr or "")
            if "nothing to commit" in combined or "nothing added to commit" in combined:
                return True
            return False
        except Exception:
            return False

    def get_decision_accumulation(self) -> int:
        """Counts unconsolidated decisions authored by explorer-tier models (FR-13)."""
        count = 0
        if not os.path.exists(self.memory_dir):
            return 0
        for filename in os.listdir(self.memory_dir):
            if not filename.endswith(".md"):
                continue
            filepath = os.path.join(self.memory_dir, filename)
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    content = f.read()
                metadata, _ = self.parse_markdown_with_yaml(content)
                if (
                    metadata.get("class") == "decision"
                    and metadata.get("consolidated") != True
                    and metadata.get("consolidated") != "true"
                ):
                    author = str(metadata.get("author_model", ""))
                    author_tier = str(metadata.get("author_tier", "")).lower()
                    is_explorer = (
                        author_tier == "explorer"
                        if author_tier
                        else ("sonnet" in author.lower() or "mini" in author.lower() or "flash" in author.lower())
                    )
                    if is_explorer:
                        count += 1
            except Exception:
                pass
        return count

    def consolidate_decisions(self, task_id: str):
        """Marks all currently unconsolidated explorer decisions as consolidated under this task boundary."""
        if not os.path.exists(self.memory_dir):
            return
        for filename in os.listdir(self.memory_dir):
            if not filename.endswith(".md"):
                continue
            filepath = os.path.join(self.memory_dir, filename)
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    content = f.read()
                metadata, body = self.parse_markdown_with_yaml(content)
                if (
                    metadata.get("class") == "decision"
                    and metadata.get("consolidated") != True
                    and metadata.get("consolidated") != "true"
                ):
                    author = str(metadata.get("author_model", ""))
                    author_tier = str(metadata.get("author_tier", "")).lower()
                    is_explorer = (
                        author_tier == "explorer"
                        if author_tier
                        else ("sonnet" in author.lower() or "mini" in author.lower() or "flash" in author.lower())
                    )
                    if is_explorer:
                        metadata["consolidated"] = True
                        new_content = self.serialize_with_yaml(metadata, body)
                        with open(filepath, "w", encoding="utf-8") as f:
                            f.write(new_content)
            except Exception:
                pass

