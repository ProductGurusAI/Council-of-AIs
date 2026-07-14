import os
import sqlite3
import re
import subprocess
import fnmatch
from typing import List, Dict, Any, Tuple

class CodebaseGraphManager:
    def __init__(self, workspace_path: str = "."):
        self.workspace_path = workspace_path
        self.db_path = os.path.join(workspace_path, ".council", "graph.db")
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._init_db()

    def _init_db(self):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS nodes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                filename TEXT UNIQUE NOT NULL,
                is_tagged INTEGER DEFAULT 0,
                reaches_tagged INTEGER DEFAULT 0
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS edges (
                parent_id INTEGER,
                child_id INTEGER,
                dependency_type TEXT,
                PRIMARY KEY (parent_id, child_id),
                FOREIGN KEY(parent_id) REFERENCES nodes(id),
                FOREIGN KEY(child_id) REFERENCES nodes(id)
            )
        """)
        conn.commit()
        conn.close()

        # Automatically add verifier_tokens.json to gitignore
        try:
            gitignore_path = os.path.join(self.workspace_path, ".gitignore")
            token_pattern = ".council/verifier_tokens.json"
            if os.path.exists(gitignore_path):
                with open(gitignore_path, "r") as f:
                    content = f.read()
                if token_pattern not in content:
                    with open(gitignore_path, "a") as f:
                        f.write(f"\n{token_pattern}\n")
            else:
                with open(gitignore_path, "w") as f:
                    f.write(f".council/memory/transcripts.db\n{token_pattern}\n")
        except Exception:
            pass

    def seed_from_workspace(self):
        """
        Walks the workspace, respects gitignore patterns, parses Python AST imports,
        tags high-stakes filenames/paths, and precomputes reaches-tagged flags.
        """
        import fnmatch
        import ast

        gitignore_patterns = []
        gitignore_path = os.path.join(self.workspace_path, ".gitignore")
        if os.path.exists(gitignore_path):
            try:
                with open(gitignore_path, "r") as f:
                    for line in f:
                        line_strip = line.strip()
                        if line_strip and not line_strip.startswith("#"):
                            gitignore_patterns.append(line_strip)
            except Exception:
                pass

        def should_ignore(path_rel: str) -> bool:
            if self._is_ignored(path_rel):
                return True
            for pat in gitignore_patterns:
                if pat.endswith("/"):
                    pat_dir = pat.rstrip("/")
                    if path_rel.startswith(pat_dir) or f"/{pat_dir}/" in path_rel:
                        return True
                else:
                    if pat in path_rel or fnmatch.fnmatch(path_rel, pat):
                        return True
            return False

        # Clear existing nodes and edges to avoid stale accumulation
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM edges")
        cursor.execute("DELETE FROM nodes")
        conn.commit()
        conn.close()

        source_files = []
        for root, dirs, files in os.walk(self.workspace_path):
            rel_root = os.path.relpath(root, self.workspace_path)
            if rel_root == ".":
                rel_root = ""
            
            # Prune directories in place to prevent entering ignored paths
            pruned_dirs = []
            for d in dirs:
                rel_dir = os.path.join(rel_root, d).replace("\\", "/").lstrip("./")
                if not should_ignore(rel_dir):
                    pruned_dirs.append(d)
            dirs[:] = pruned_dirs

            for file in files:
                rel_file = os.path.join(rel_root, file).replace("\\", "/").lstrip("./")
                if should_ignore(rel_file):
                    continue
                if file.lower().endswith(self.SOURCE_EXTENSIONS):
                    source_files.append(rel_file)

        high_stakes_keywords = ["schema", "auth", "migration", "config", "api", "models", "security"]
        
        # Add nodes
        for file in source_files:
            filename = os.path.basename(file)
            is_tagged = any(k in file.lower() or k in filename.lower() for k in high_stakes_keywords)
            self.add_node(file, is_tagged=is_tagged)

        # Build lookup maps for resolving imports
        base_to_full = {}
        for file in source_files:
            filename = os.path.basename(file)
            base_to_full[filename] = file
            name_no_ext = os.path.splitext(filename)[0]
            base_to_full[name_no_ext] = file

        # Parse dependencies
        for file in source_files:
            abs_filepath = os.path.join(self.workspace_path, file)
            if file.lower().endswith(".py"):
                try:
                    with open(abs_filepath, "r", encoding="utf-8", errors="ignore") as f:
                        tree = ast.parse(f.read(), filename=file)
                    
                    for node in ast.walk(tree):
                        if isinstance(node, ast.Import):
                            for name in node.names:
                                parts = name.name.split(".")
                                for part in parts:
                                    if part in base_to_full:
                                        self.add_edge(file, base_to_full[part], dep_type="import")
                        elif isinstance(node, ast.ImportFrom):
                            if node.module:
                                parts = node.module.split(".")
                                for part in parts:
                                    if part in base_to_full:
                                        self.add_edge(file, base_to_full[part], dep_type="import")
                except Exception:
                    pass
            else:
                try:
                    with open(abs_filepath, "r", encoding="utf-8", errors="ignore") as f:
                        content = f.read()
                    matches = re.findall(r"(?:import|require|from)\s+['\"]([^'\"]+)['\"]", content)
                    for m in matches:
                        imported_base = os.path.basename(m)
                        imported_no_ext = os.path.splitext(imported_base)[0]
                        if imported_base in base_to_full:
                            self.add_edge(file, base_to_full[imported_base], dep_type="import")
                        elif imported_no_ext in base_to_full:
                            self.add_edge(file, base_to_full[imported_no_ext], dep_type="import")
                except Exception:
                    pass

        self.precompute_reachability(k_hops=2)


    def add_node(self, filename: str, is_tagged: bool = False):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute(
            "INSERT OR REPLACE INTO nodes (filename, is_tagged) VALUES (?, ?)",
            (filename, 1 if is_tagged else 0)
        )
        conn.commit()
        conn.close()

    def add_edge(self, parent_file: str, child_file: str, dep_type: str = "import"):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        # Ensure nodes exist
        cursor.execute("INSERT OR IGNORE INTO nodes (filename) VALUES (?)", (parent_file,))
        cursor.execute("INSERT OR IGNORE INTO nodes (filename) VALUES (?)", (child_file,))
        
        # Get IDs
        cursor.execute("SELECT id FROM nodes WHERE filename = ?", (parent_file,))
        parent_id = cursor.fetchone()[0]
        cursor.execute("SELECT id FROM nodes WHERE filename = ?", (child_file,))
        child_id = cursor.fetchone()[0]
        
        cursor.execute(
            "INSERT OR REPLACE INTO edges (parent_id, child_id, dependency_type) VALUES (?, ?, ?)",
            (parent_id, child_id, dep_type)
        )
        conn.commit()
        conn.close()

    def precompute_reachability(self, k_hops: int = 2):
        """
        Precomputes reachability to any tagged high-stakes node within k_hops.
        Optimized by starting from tagged nodes and traversing backwards.
        """
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        # 1. Reset all reaches_tagged flags
        cursor.execute("UPDATE nodes SET reaches_tagged = 0")
        
        # 2. Get initial set of tagged nodes
        cursor.execute("SELECT id FROM nodes WHERE is_tagged = 1")
        tagged_nodes = {row[0] for row in cursor.fetchall()}
        
        reaching_nodes = set(tagged_nodes)
        current_set = set(tagged_nodes)
        
        # 3. Traverse backward along edges: child -> parent (since parent depends on child)
        for _ in range(k_hops):
            if not current_set:
                break
            placeholders = ",".join("?" for _ in current_set)
            query = f"SELECT parent_id FROM edges WHERE child_id IN ({placeholders})"
            cursor.execute(query, list(current_set))
            parents = {row[0] for row in cursor.fetchall()}
            
            # Find new parents we haven't visited yet
            new_parents = parents - reaching_nodes
            reaching_nodes.update(new_parents)
            current_set = new_parents

        # 4. Write back to database
        if reaching_nodes:
            placeholders = ",".join("?" for _ in reaching_nodes)
            cursor.execute(f"UPDATE nodes SET reaches_tagged = 1 WHERE id IN ({placeholders})", list(reaching_nodes))
            
        conn.commit()
        conn.close()

    # The Council's own artifacts and non-source noise. Without this ignore list,
    # ledger_store.json / .council/ files trip the fail-safe on EVERY task and
    # route everything to the Thinker (fail-safe becomes fail-expensive).
    IGNORE_PREFIXES = (".council/", ".git/", "tasks_store/", "refs/", "__pycache__/")
    IGNORE_FILES = ("ledger_store.json", "models.json", ".env", ".gitignore", ".DS_Store")
    SOURCE_EXTENSIONS = (
        ".py", ".js", ".jsx", ".ts", ".tsx", ".go", ".rs", ".java", ".rb", ".php",
        ".c", ".h", ".cpp", ".hpp", ".cs", ".swift", ".kt", ".sql", ".sh",
        ".yaml", ".yml", ".toml", ".proto", ".graphql", ".tf"
    )

    def _is_ignored(self, filepath: str) -> bool:
        norm = filepath.replace("\\", "/").lstrip("./")
        if os.path.basename(norm) in self.IGNORE_FILES:
            return True
        return any(norm.startswith(p) for p in self.IGNORE_PREFIXES)

    def get_git_modified_files(self) -> List[Tuple[str, bool]]:
        """
        Parses `git status --porcelain` and returns (filepath, is_untracked) pairs,
        with the Council's own artifacts filtered out.
        """
        try:
            res = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=self.workspace_path, capture_output=True, text=True, check=True
            )
            files = []
            for line in res.stdout.split("\n"):
                if not line.strip():
                    continue
                # Porcelain shape: "XY path" — first two chars are status codes
                status, _, path = line[:2], line[2], line[3:].strip()
                if not path or self._is_ignored(path):
                    continue
                files.append((path, status == "??"))
            return files
        except Exception:
            return []

    def check_touch_list(self, modified_files: List[Any]) -> bool:
        """
        Checks if any modified files touch high-stakes paths.

        Fail-safe policy (PRD FR-12/12a):
        - Known nodes: escalate if tagged or reaches a tagged node.
        - Unknown SOURCE files: escalate (unknown blast radius = high stakes).
        - Unknown non-source files (notes, data, scratch): do not escalate.
        - Council artifacts: always ignored (see IGNORE_*).

        Accepts plain paths or (path, is_untracked) pairs from get_git_modified_files.
        """
        if not modified_files:
            return False

        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        try:
            for entry in modified_files:
                file = entry[0] if isinstance(entry, (tuple, list)) else entry
                if self._is_ignored(file):
                    continue
                filename = os.path.basename(file)
                cursor.execute("SELECT is_tagged, reaches_tagged FROM nodes WHERE filename = ? OR filename = ?", (file, filename))
                row = cursor.fetchone()
                if not row:
                    # Unknown node: fail-safe to escalate ONLY for source files —
                    # unknown code has unknown blast radius; unknown notes do not.
                    if filename.lower().endswith(self.SOURCE_EXTENSIONS):
                        return True
                    continue
                is_tagged, reaches_tagged = row
                if is_tagged == 1 or reaches_tagged == 1:
                    return True
            return False
        finally:
            conn.close()


class GateCascade:
    def __init__(self, graph_manager: CodebaseGraphManager):
        self.graph_manager = graph_manager

    def route(self, prompt: str, modified_files: List[str] = None, decision_accumulation: int = 0) -> str:
        """
        Sequentially runs routing gates.
        Returns: 'thinker' or 'explorer'
        """
        prompt_lower = prompt.lower()
        modified_files = modified_files or []

        # --- Gate 0: Override ---
        if "!think" in prompt_lower:
            return "thinker"
        if "!cheap" in prompt_lower:
            return "explorer"

        # --- Gate 1: Stakes Check ---
        # 1. Structural check on modified files (precomputed reachability)
        if self.graph_manager.check_touch_list(modified_files):
            return "thinker"

        # 2. Key security/auth phrases check
        stakes_keywords = [
            r"\bsecurity\b", r"\bcredentials?\b", r"\bauth(entication)?\b",
            r"\bdeploy(ment)?\b", r"\brelease\b", r"\bcontract\b", r"\bschema\b"
        ]
        for pattern in stakes_keywords:
            if re.search(pattern, prompt_lower):
                return "thinker"

        # --- Gate 2: Dependency / Trade-off Check ---
        # 1. Structural check on decisions accumulation
        if decision_accumulation >= 5:
            return "thinker"

        # 2. Complex context keywords check
        dependency_keywords = [
            r"\breconcile\b", r"\btrade-off\b", r"\brefactor(ing)?\b",
            r"\baudit\b", r"\bcross-component\b", r"\bdebugging\b"
        ]
        for pattern in dependency_keywords:
            if re.search(pattern, prompt_lower):
                return "thinker"

        # --- Gate 3: Volume Check ---
        volume_keywords = [
            r"\bsummarize\b", r"\bread\b", r"\bdraft\b", r"\bbrainstorm\b", r"\blist\b"
        ]
        for pattern in volume_keywords:
            if re.search(pattern, prompt_lower):
                return "explorer"

        # --- Gate 4: Everything Else ---
        # Ambiguous tasks default to explorer (cheap-first) with verification gates
        return "explorer"
