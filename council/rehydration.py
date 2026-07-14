import os
import re
import subprocess
from typing import List, Dict, Any, Tuple

class RehydrationTester:
    def __init__(self, client=None, scorer_model: str = "claude-3-haiku-20240307"):
        self.client = client
        self.scorer_model = scorer_model

    def generate_quiz(self, transcripts: List[Dict[str, Any]]) -> List[Dict[str, str]]:
        """
        Generates 10 questions from the raw archived transcripts.
        In a real run, this would ask an LLM to generate questions.
        For Phase 2/Spine core, we generate structural quiz templates.
        """
        quiz = []
        # Fallback templates if transcripts are empty
        if not transcripts:
            return [{"question": "What is the primary project goal?", "answer": "Council of AIs"}]

        # Generate questions based on actual transcript task turns
        for i, t in enumerate(transcripts[:10]):
            content_snippet = t.get("content", "")[:60]
            task_id = t.get("task_id", "unknown-task")
            quiz.append({
                "question": f"In task {task_id}, what was the intent behind the query: '{content_snippet}'?",
                "answer": t.get("content", "")
            })
        
        # Ensure we have at least some questions
        while len(quiz) < 10 and transcripts:
            quiz.append(quiz[0])
            
        return quiz[:10]

    def score_quiz(self, memory_snapshot: str, quiz: List[Dict[str, str]], provider: str = "anthropic", tier: str = "cheap") -> float:
        """
        Scores the model's quiz answers given only the memory snapshot.
        If no client is configured, we run in simulated quiz mode.
        Returns: score float between 0.0 and 1.0 (e.g. 0.8 = 8/10).
        """
        if not self.client or self.client.is_simulated:
            # Simulation Mode: if 'Invariants' is in snapshot, mock high score; otherwise low score
            if "INVARIANTS" in memory_snapshot or "invariants" in memory_snapshot.lower():
                return 0.9
            return 0.5

        # Real scoring loop using the Unified Client
        correct = 0
        for item in quiz:
            q = item["question"]
            correct_ans = item["answer"]
            
            # 1. Ask model to answer the question using ONLY the memory snapshot
            prompt = (
                f"Context (Memory Snapshot):\n{memory_snapshot}\n\n"
                f"Question: {q}\n"
                "Answer the question based strictly on the context provided."
            )
            
            # Create a clean temporary CacheEnvelope with only the snapshot to prevent leakages
            from council.client import CacheEnvelope
            env = CacheEnvelope(memory_snapshot, "", "")
            
            try:
                # Execute answer generation on cheap model
                ans_response, _ = self.client.execute_task(
                    task_id="quiz-run",
                    tier=tier,
                    provider=provider,
                    cache_envelope=env,
                    user_prompt=prompt,
                    warm_cache=False
                )
                
                # 2. Ask cheap model to grade the generated answer against the correct answer
                grading_prompt = (
                    f"Correct Answer: {correct_ans}\n"
                    f"Model Answer: {ans_response}\n\n"
                    "Does the Model Answer capture the core meaning of the Correct Answer? "
                    "Output ONLY '1' for Yes, or '0' for No."
                )
                
                grade_env = CacheEnvelope("You are a grading assistant.", "", "")
                grade_response, _ = self.client.execute_task(
                    task_id="quiz-grade",
                    tier=tier,
                    provider=provider,
                    cache_envelope=grade_env,
                    user_prompt=grading_prompt,
                    warm_cache=False
                )
                
                if "1" in grade_response:
                    correct += 1
            except Exception:
                # Fallback to safe zero on API error
                pass
                
        return correct / len(quiz) if quiz else 0.0


class BisectRecovery:
    def __init__(self, workspace_path: str):
        self.workspace_path = workspace_path
        self.lock_file = os.path.join(workspace_path, ".council", "memory", "write_lockout.lock")

    def is_locked(self) -> bool:
        return os.path.exists(self.lock_file)

    def acquire_lock(self, bad_commit: str, reason: str):
        os.makedirs(os.path.dirname(self.lock_file), exist_ok=True)
        with open(self.lock_file, "w") as f:
            f.write(f"LOCKOUT_COMMIT: {bad_commit}\nREASON: {reason}\n")

    def release_lock(self):
        if os.path.exists(self.lock_file):
            os.remove(self.lock_file)

    def run_git(self, args: List[str]) -> str:
        res = subprocess.run(["git"] + args, cwd=self.workspace_path, capture_output=True, text=True, check=True)
        return res.stdout.strip()

    def bisect_and_revert(self, last_passing_commit: str, quizzer: RehydrationTester, quiz_questions: List[Dict[str, str]]) -> Tuple[str, str]:
        """
        Performs a git bisect between HEAD (bad) and last_passing_commit (good)
        to identify the specific commit that introduced rehydration failure.
        Surgically reverts the bad commit and write-locks the repo.
        
        Returns: (first_bad_commit_hash, revert_output_log)
        """
        if not last_passing_commit:
            raise ValueError("A valid last_passing_commit is required to bisect.")

        # Save current branch name to return later
        current_branch = self.run_git(["rev-parse", "--abbrev-ref", "HEAD"])
        
        # 1. Start git bisect
        self.run_git(["bisect", "start"])
        self.run_git(["bisect", "bad", "HEAD"])
        self.run_git(["bisect", "good", last_passing_commit])

        first_bad_commit = ""
        last_bad_candidate = ""  # tracked so the fallback never points at post-reset HEAD
        try:
            while True:
                # Check current bisect commit
                bisect_status = self.run_git(["status"])
                # Extract active bisect commit hash
                current_commit = self.run_git(["rev-parse", "HEAD"])
                
                # Load memory state at this commit
                memory_snapshot = ""
                memory_dir = os.path.join(self.workspace_path, ".council", "memory")
                for filename in ["invariants.md", "decisions.md", "questions.md", "persona.md"]:
                    path = os.path.join(memory_dir, filename)
                    if os.path.exists(path):
                        with open(path, "r") as f:
                            memory_snapshot += f.read() + "\n"

                # Run quiz against memory snapshot
                score = quizzer.score_quiz(memory_snapshot, quiz_questions)
                
                if score >= 0.8:
                    # Good commit
                    bisect_out = self.run_git(["bisect", "good"])
                else:
                    # Bad commit
                    last_bad_candidate = current_commit
                    bisect_out = self.run_git(["bisect", "bad"])

                # Check if bisect finished
                if "is the first bad commit" in bisect_out or "first bad commit" in bisect_out.lower():
                    # Parse the bad commit hash from bisect output
                    # Output shape: "commit_hash is the first bad commit"
                    match = re.search(r"([a-f0-9]{40}) is the first bad commit", bisect_out)
                    if match:
                        first_bad_commit = match.group(1)
                    else:
                        first_bad_commit = current_commit
                    break
        finally:
            # End bisect and restore working directory to original branch state
            self.run_git(["bisect", "reset"])

        if not first_bad_commit:
            # Fall back to the last commit that actually scored bad during the
            # bisect — post-reset HEAD is the branch tip, not the culprit.
            first_bad_commit = last_bad_candidate or self.run_git(["rev-parse", "HEAD"])

        # 2. Surgical Revert of the first bad commit
        revert_log = ""
        try:
            # Surgical git revert preserves subsequent task commits
            # --no-edit is used to bypass interactive editor prompts
            revert_log = self.run_git(["revert", "--no-edit", first_bad_commit])
        except subprocess.CalledProcessError as err:
            # Revert conflict occurred
            revert_log = f"REVERT_CONFLICT:\n{err.stderr}\n{err.stdout}"
            # Abort revert to clean index
            self.run_git(["revert", "--abort"])
            
            # Quarantine the conflicting files by checking them out from the bad commit
            quarantine_dir = os.path.join(self.workspace_path, ".council", "quarantine")
            os.makedirs(quarantine_dir, exist_ok=True)
            
            # Copy memory files from bad commit for debugging forensics
            for filename in ["invariants.md", "decisions.md", "questions.md", "persona.md"]:
                try:
                    content = self.run_git(["show", f"{first_bad_commit}:.council/memory/{filename}"])
                    with open(os.path.join(quarantine_dir, f"conflict_{filename}"), "w") as f:
                        f.write(content)
                except Exception:
                    pass

        # 3. Lock write capability
        lock_reason = f"Rehydration test failed (< 0.8). Offending commit: {first_bad_commit}."
        if "REVERT_CONFLICT" in revert_log:
            lock_reason += " Revert conflicted and was aborted; memory quarantined."
        else:
            lock_reason += " Revert completed successfully."

        self.acquire_lock(
            bad_commit=first_bad_commit,
            reason=lock_reason
        )

        return first_bad_commit, revert_log
