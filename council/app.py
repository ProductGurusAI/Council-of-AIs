import os
import json
import uuid
import click
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.prompt import Prompt
from dotenv import load_dotenv

from council.ledger import Ledger
from council.client import UnifiedClient, CacheEnvelope
from council.bypass import BypassLane
from council.gates import GateCascade, CodebaseGraphManager
from council.compactor import ContextCompactor
from council.memory import MemoryWrapper
from council.escalation import CleanRoomEscalator
from council.verifier import VerificationRunner

load_dotenv()

console = Console()

# Path for task storage
TASKS_DIR = "tasks_store"
os.makedirs(TASKS_DIR, exist_ok=True)


def save_task_envelope(task_id: str, envelope: CacheEnvelope):
    filepath = os.path.join(TASKS_DIR, f"{task_id}.json")
    with open(filepath, "w") as f:
        json.dump({
            "layer1": envelope.layer1,
            "layer2": envelope.layer2,
            "layer3": envelope.layer3,
            "layer4_turns": envelope.layer4_turns
        }, f, indent=4)


def load_task_envelope(task_id: str) -> CacheEnvelope:
    filepath = os.path.join(TASKS_DIR, f"{task_id}.json")
    if not os.path.exists(filepath):
        raise FileNotFoundError(f"Task {task_id} not found.")
    with open(filepath, "r") as f:
        data = json.load(f)
    env = CacheEnvelope(data["layer1"], data["layer2"], data["layer3"])
    env.layer4_turns = data["layer4_turns"]
    return env


def _new_task(goal: str) -> str:
    task_id = f"task-{uuid.uuid4().hex[:8]}"
    system_prompt = (
        "You are an assistant inside the Council of AIs workbench.\n"
        "INVARIANTS: Never suggest subscription upgrades. Keep outputs precise."
    )
    decisions = "No prior decisions made."
    task_canvas = f"Goal: {goal}\nCanvas: [Init]"
    envelope = CacheEnvelope(system_prompt, decisions, task_canvas)
    save_task_envelope(task_id, envelope)
    return task_id


class CouncilSession:
    """
    The integrated turn pipeline. Wires together every subsystem:
      override -> bypass -> gate cascade (with codebase graph) -> runaway check
      -> compactor (50%/85%) -> unified client -> ledger receipt
      -> memory transcript (L0) -> git task-boundary commit on close.
    """

    def __init__(self, workspace_path: str = "."):
        # Ledger paths MUST anchor to the workspace, not the process CWD.
        # A dashboard server launched from another directory otherwise loads a
        # fresh default models.json and silently ignores the user's saved tier
        # mappings (symptom: "I picked Fable 5 but it ran Opus").
        self.ledger = Ledger(
            filepath=os.path.join(workspace_path, "ledger_store.json"),
            pricing_config=os.path.join(workspace_path, "models.json"),
        )
        self.client = UnifiedClient(self.ledger)
        self.bypass_lane = BypassLane()
        self.graph = CodebaseGraphManager(workspace_path)
        self.gates = GateCascade(self.graph)
        self.compactor = ContextCompactor()
        self.memory = MemoryWrapper(workspace_path)
        self.escalator = CleanRoomEscalator(self.client)
        self.verifier = VerificationRunner(workspace_path)
        self._turn_index = {}
        self._user_confirmed = {}
        self._thinker_tasks = set()  # tasks where a thinker-tier turn actually ran (FR-13)

        # FR-12a: Graph auto-seeding on init
        try:
            import sqlite3
            conn = sqlite3.connect(self.graph.db_path)
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM nodes")
            cnt = cursor.fetchone()[0]
            conn.close()
            if cnt == 0:
                self.graph.seed_from_workspace()
        except Exception:
            pass

    def route(self, prompt: str):
        """
        Returns (tier, user_prompt, route_reason).
        Order: explicit override -> bypass lane -> full gate cascade.
        """
        user_prompt = prompt.strip()

        # Gate 0: explicit override
        if user_prompt.startswith("!think"):
            return "thinker", user_prompt.replace("!think", "", 1).strip(), "Override (!think)"
        if user_prompt.startswith("!cheap"):
            return "cheap", user_prompt.replace("!cheap", "", 1).strip(), "Override (!cheap)"

        # Bypass lane for trivial one-shots
        is_bypassed, bypass_tier, _ = self.bypass_lane.classify(user_prompt, touch_list=[])
        if is_bypassed:
            return "cheap", user_prompt, f"Bypass Lane ({bypass_tier})"

        # Full gate cascade with codebase-graph blast-radius check (FR-12/12a)
        modified = self.graph.get_git_modified_files()
        # FR-13: Decision accumulation check
        decisions_accumulated = self.memory.get_decision_accumulation()
        tier = self.gates.route(user_prompt, modified_files=modified, decision_accumulation=decisions_accumulated)
        return tier, user_prompt, f"Gate Cascade -> {tier}"

    def execute_turn(self, task_id: str, prompt: str, provider: str = "anthropic"):
        """
        Runs one full Council turn. Returns (response, cost, tier, route_reason)
        or raises PermissionError on budget halt / RuntimeError on runaway halt.
        """
        tier, user_prompt, route_reason = self.route(prompt)
        is_thinker = (tier == "thinker")
        est_cost = 0.05 if is_thinker else 0.005

        # FR-8: endgame confirmation is SINGLE-USE — pop it now so one "yes"
        # approves exactly one thinker spend, and thread it through both the
        # pre-check here and the client's internal check.
        confirmed = bool(self._user_confirmed.pop(task_id, False)) if is_thinker else False

        # Check constraints including FR-8 Endgame confirmation
        is_allowed, reason = self.ledger.check_constraints(
            task_id=task_id,
            next_step_est_cost=est_cost,
            is_thinker_escalation=is_thinker,
            user_confirmed=confirmed
        )
        if not is_allowed:
            raise PermissionError(reason)

        # Runaway protection (FR-9)
        self.ledger.record_state(task_id, f"run-{tier}")
        if self.ledger.check_runaway_loop(task_id):
            raise RuntimeError(
                "RUNAWAY PROTECTION HALT: loop exceeded 3 iterations without progress state change."
            )

        envelope = load_task_envelope(task_id)
        warm_cache = len(envelope.layer4_turns) > 0

        # Context compaction (FR-28/29) BEFORE execution
        pressure = self.compactor.check_limits(envelope)
        if pressure == "mild":
            self.compactor.compact_mild(envelope)
        elif pressure == "aggressive":
            did_reset, note = self.compactor.compact_aggressive(envelope, self.ledger.calculate_cost)
            if did_reset:
                warm_cache = False  # cache epoch reset: pay the rebuild deliberately
            console.print(f"[dim]{note}[/dim]")

        before_spend = self.ledger.get_task_spend(task_id)

        response, cost = self.client.execute_task(
            task_id=task_id,
            tier=tier,
            provider=provider,
            cache_envelope=envelope,
            user_prompt=user_prompt,
            warm_cache=warm_cache,
            user_confirmed=confirmed
        )

        # Run verification checks if a gate exists and it's an explorer/cheap tier
        gate_file = self.verifier.completion_gate._get_filepath(task_id)
        if os.path.exists(gate_file) and tier in ("explorer", "cheap"):
            pre_attempt_snapshot = envelope.layer1
            pre_attempt_decisions = envelope.layer2
            
            passed, verify_msg = self.verifier.run_checks(
                task_id=task_id,
                client=self.client,
                pre_attempt_snapshot=pre_attempt_snapshot,
                pre_attempt_decisions=pre_attempt_decisions,
                original_prompt=user_prompt,
                failed_output=response,
                provider=provider
            )
            
            if not passed:
                escalation_resp = getattr(self.verifier, "last_escalation_response", None) or "[Escalation failed or blocked]"
                
                # Replace failed explorer response with thinker response in envelope
                if envelope.layer4_turns and envelope.layer4_turns[-1]["role"] == "assistant":
                    envelope.layer4_turns[-1]["content"] = escalation_resp
                else:
                    envelope.add_turn("assistant", escalation_resp)
                
                response = escalation_resp
                tier = "thinker"
                route_reason += " -> Escalated to Thinker"
                self._thinker_tasks.add(task_id)

        save_task_envelope(task_id, envelope)

        after_spend = self.ledger.get_task_spend(task_id)
        cost = after_spend - before_spend

        # FR-13: remember that a thinker actually ran in this task — only such
        # tasks may consolidate explorer decisions at close.
        if tier == "thinker":
            self._thinker_tasks.add(task_id)

        # L0 raw transcript archive (FR-22/27).
        # Turn index must survive process restarts: the dashboard spawns a fresh
        # CouncilSession per request, so an in-memory counter resets to 0 and
        # every turn collides at index 0/1 (this was the cause of the
        # questions-grouped/answers-grouped chat bug). Seed from the DB.
        if task_id not in self._turn_index:
            try:
                import sqlite3 as _sq
                conn = _sq.connect(self.memory.db_path)
                row = conn.execute(
                    "SELECT COALESCE(MAX(turn_index), -1) FROM transcripts WHERE task_id = ?",
                    (task_id,)
                ).fetchone()
                conn.close()
                self._turn_index[task_id] = (row[0] if row else -1) + 1
            except Exception:
                self._turn_index[task_id] = 0
        idx = self._turn_index.get(task_id, 0)
        model_used = self.client.select_model(tier, provider)
        self.memory.append_transcript(task_id, idx, "user", user_prompt)
        self.memory.append_transcript(task_id, idx + 1, "assistant", response, cost=cost, model=model_used, tier=tier)
        self._turn_index[task_id] = idx + 2

        # §10 Routing Analytics Logger
        try:
            from council.analytics import AnalyticsLogger
            import re
            logger = AnalyticsLogger(self.graph.workspace_path)
            logger.log_turn(
                task_id=task_id,
                prompt_features=re.findall(r"\w+", prompt.lower())[:10],
                gate_fired=route_reason,
                tier=tier,
                cost=cost,
                override_used=prompt.strip().startswith("!"),
                # The turn itself is not a verification event; the
                # VerificationRunner logs real pass/fail outcomes itself.
                verification_result="unverified"
            )
        except Exception:
            pass

        return response, cost, tier, route_reason

    def generate_and_save_handoff(self, task_id: str, last_tier: str = "explorer", provider: str = "anthropic"):
        """
        Prompts the executing model tier to write a handoff note.
        Validates and saves the handoff markdown file (§7.2).
        """
        import re
        model_name = self.client.select_model(last_tier, provider)

        if self.client.is_simulated:
            # Simulated mode handoff
            note_content = (
                "---\n"
                f"class: progress\n"
                f"author_model: {model_name}\n"
                f"author_tier: {last_tier}\n"
                f"task_id: {task_id}\n"
                "---\n"
                "Simulated progress update: task execution completed."
            )
        else:
            envelope = load_task_envelope(task_id)
            system_prompt = (
                "You are a Council consolidation agent. Generate a task handoff entry summarizing the decisions and progress.\n"
                "The output MUST be Markdown starting with YAML front-matter delimited by ---."
            )
            
            user_prompt = (
                "Based on the conversation history in Layer 4, write a handoff file in Markdown starting with YAML front-matter.\n"
                "Front-matter MUST contain:\n"
                "  class: (invariant, decision, progress)\n"
                "  author_model: (string model name)\n"
                "  author_tier: (thinker, explorer, cheap)\n"
                "  task_id: (the current task ID)\n"
                "  reopen_condition: (only required if class is 'decision')\n\n"
                "Note: Cheap/Explorer models (e.g. gpt-4o-mini, claude-3-5-sonnet, gemini-1.5-flash) can ONLY write 'progress' class. "
                "Only Thinker models (claude-3-opus) can write 'decision' or 'invariant'. Output the handoff file now."
            )
            
            handoff_env = CacheEnvelope(system_prompt, envelope.layer2, envelope.layer3)
            handoff_env.layer4_turns = envelope.layer4_turns
            
            try:
                note_content, _ = self.client.execute_task(
                    task_id=f"handoff-{task_id}",
                    tier=last_tier,
                    provider=provider,
                    cache_envelope=handoff_env,
                    user_prompt=user_prompt,
                    warm_cache=False
                )
                match = re.search(r"(---.*?---.*)", note_content, re.DOTALL)
                if match:
                    note_content = match.group(1)
            except Exception as e:
                console.print(f"[bold yellow]Handoff generation failed: {str(e)}. Defaulting fallback...[/bold yellow]")
                note_content = (
                    "---\n"
                    f"class: progress\n"
                    f"author_model: {model_name}\n"
                    f"author_tier: {last_tier}\n"
                    f"task_id: {task_id}\n"
                    "---\n"
                    "Default fallback update: task execution completed."
                )

        try:
            filename = f"{task_id}_handoff.md"
            self.memory.validate_and_save_entry(filename, note_content)
            console.print(f"[bold green]Handoff note saved to memory as {filename}[/bold green]")
        except Exception as e:
            console.print(f"[bold yellow]Handoff entry rejected by validations: {str(e)}[/bold yellow]")

    def close_task(self, task_id: str):
        """Task boundary: commit memory state to git, tagged with the task id (FR-24)."""
        # FR-13: consolidation may ONLY happen after a Thinker consolidation
        # pass. An ordinary task close must never reset the accumulation
        # counter, or the counter can never force the consolidation it exists
        # to force.
        if task_id in self._thinker_tasks:
            try:
                self.memory.consolidate_decisions(task_id)
            except Exception:
                pass
        committed = self.memory.commit_task_boundary(task_id)
        if not committed:
            console.print(
                "[bold yellow]⚠ FR-24 WARNING: memory git commit failed — the audit trail "
                "did not record this task boundary. Check git identity/repo state.[/bold yellow]"
            )
        else:
            # Optional non-blocking push to remote if configured (Fable 5 suggestion)
            try:
                import subprocess
                remotes_res = subprocess.run(["git", "remote"], cwd=self.workspace_path, capture_output=True, text=True)
                if remotes_res.returncode == 0 and remotes_res.stdout.strip():
                    # Attempt push (branch and tags)
                    push_res = subprocess.run(["git", "push"], cwd=self.workspace_path, capture_output=True, text=True)
                    push_tags_res = subprocess.run(["git", "push", "--tags"], cwd=self.workspace_path, capture_output=True, text=True)
                    if push_res.returncode != 0 or push_tags_res.returncode != 0:
                        console.print(
                            "[bold yellow]⚠ WARNING: Push to remote repository failed (remote may be offline or unreachable). "
                            "Local task boundary committed successfully.[/bold yellow]"
                        )
            except Exception:
                pass


def _print_receipt(session: CouncilSession, task_id: str, tier: str, provider: str, cost: float):
    table = Table(title="Ledger Receipt", show_header=True, header_style="bold magenta")
    table.add_column("Metric", style="dim")
    table.add_column("Value", justify="right")
    table.add_row("Model Used", session.client.select_model(tier, provider))
    table.add_row("Turn Cost", f"${cost:.5f}")
    table.add_row("Task Total Spend", f"${session.ledger.get_task_spend(task_id):.5f}")
    table.add_row("Prepaid Budget Remaining", f"${session.ledger.get_remaining_budget():.5f}")
    console.print(table)


@click.group(invoke_without_command=True)
@click.pass_context
def cli(ctx):
    """Council of AIs CLI — Orchestrate models under strict budget limits."""
    if ctx.invoked_subcommand is None:
        ctx.forward(interactive)


@cli.command()
@click.argument("goal")
@click.option("--criteria", help="Comma-separated list of verification criteria (prefix with 'cmd:' for shell commands)")
def start_task(goal: str, criteria: str = None):
    """Starts a new Task and initializes the cache envelope."""
    task_id = _new_task(goal)
    console.print(f"[bold green]Successfully initialized Task:[/bold green] {task_id}")
    console.print(f"[bold blue]Goal:[/bold blue] {goal}")
    
    if criteria:
        session = CouncilSession()
        items = [c.strip() for c in criteria.split(",") if c.strip()]
        if items:
            session.verifier.create_for_task(task_id, items)
            console.print(f"[bold green]Verification completion gate created with {len(items)} items.[/bold green]")
            
    return task_id


@cli.command()
@click.argument("task_id")
@click.argument("prompt")
@click.option("--provider", type=click.Choice(["anthropic", "openai", "gemini"]), default="anthropic")
def run_turn(task_id: str, prompt: str, provider: str):
    """Executes a single turn within an open task under budget controls."""
    session = CouncilSession()
    try:
        response, cost, tier, route_reason = session.execute_turn(task_id, prompt, provider)
    except FileNotFoundError:
        console.print(f"[bold red]Error:[/bold red] Task {task_id} does not exist. Use start-task first.", err=True)
        return
    except PermissionError as pe:
        if "ENDGAME" in str(pe):
            if Prompt.ask("[bold yellow]Endgame mode trigger. Confirm Thinker spend? (y/n)[/bold yellow]", default="n").lower() == "y":
                session._user_confirmed[task_id] = True
                try:
                    response, cost, tier, route_reason = session.execute_turn(task_id, prompt, provider)
                except Exception as err:
                    console.print(f"[bold red]Halt: {str(err)}[/bold red]", err=True)
                    session.close_task(task_id)
                    return
            else:
                console.print("[bold red]Thinker spend rejected by user. Task aborted.[/bold red]")
                session.close_task(task_id)
                return
        else:
            console.print(f"\n[bold red]BUDGET HALT:[/bold red] {str(pe)}", err=True)
            session.close_task(task_id)
            return
    except RuntimeError as re_err:
        console.print(f"\n[bold red]{str(re_err)}[/bold red]", err=True)
        return

    console.print(f"[bold blue]Route:[/bold blue] {route_reason} (via {provider})")
    console.print("\n[bold green]Response:[/bold green]")
    console.print(response)
    console.print("")
    _print_receipt(session, task_id, tier, provider, cost)


@cli.command()
def ledger():
    """Prints the complete budget usage history and active tasks."""
    ledger = Ledger()
    remaining = ledger.get_remaining_budget()
    total_spent = ledger.data["total_spent"]

    console.print(Panel(
        f"[bold cyan]Council of AIs Ledger Dashboard[/bold cyan]\n\n"
        f"Total Prepaid Pool:  ${ledger.total_budget:.2f}\n"
        f"Total Amount Spent:  ${total_spent:.4f}\n"
        f"Budget Remaining:    [bold green]${remaining:.4f}[/bold green]\n"
        f"Reserve Floor Limit: ${ledger.reserve_floor:.2f} (locked for Thinker)\n"
        f"Per-Task Limit Cap:  ${ledger.per_task_cap:.2f}"
    ))

    unverified = ledger.get_unverified_models()
    if unverified:
        console.print(
            f"[bold yellow]⚠ Unverified prices in models.json:[/bold yellow] {', '.join(unverified)}\n"
            f"[dim]Verify against provider pricing pages before funding a real budget.[/dim]"
        )

    tasks = ledger.data.get("tasks", {})
    if tasks:
        table = Table(title="Task Spending History", show_header=True, header_style="bold blue")
        table.add_column("Task ID")
        table.add_column("Spend Amount", justify="right")
        table.add_column("Turn Count", justify="right")

        for tid, tdata in tasks.items():
            table.add_row(tid, f"${tdata['total_spent']:.5f}", str(len(tdata['transactions'])))
        console.print(table)
    else:
        console.print("[dim]No tasks recorded in ledger yet.[/dim]")


@cli.command()
def interactive():
    """Launches the interactive task workbench console."""
    console.print(Panel(
        "Welcome to the [bold purple]Council of AIs Workbench[/bold purple]\n"
        "Start a task by typing your goal, then iterate on it under real-time budget tracking.\n"
        "Overrides: Type [bold cyan]!think <prompt>[/bold cyan] or [bold cyan]!cheap <prompt>[/bold cyan] to override routing.\n"
        "Type 'exit' to close the task (commits memory to git) and quit.",
        title="Interactive Session"
    ))

    goal = Prompt.ask("[bold green]Enter your Task Goal[/bold green]")
    if goal.lower().strip() == "exit":
        return

    session = CouncilSession()
    task_id = _new_task(goal)
    console.print(f"[bold green]Initialized workspace task ID:[/bold green] {task_id}\n")

    criteria_input = Prompt.ask("[bold cyan]Enter optional verification criteria (comma-separated, prefix with 'cmd:' for shell commands)[/bold cyan]", default="")
    if criteria_input.strip():
        items = [c.strip() for c in criteria_input.split(",") if c.strip()]
        if items:
            session.verifier.create_for_task(task_id, items)
            console.print(f"[bold green]Verification completion gate created with {len(items)} items.[/bold green]\n")

    last_used_tier = "explorer"
    while True:
        prompt = Prompt.ask(f"[bold yellow]{task_id}[/bold yellow] >")
        if prompt.strip().lower() == "exit":
            session.generate_and_save_handoff(task_id, last_tier=last_used_tier)
            session.close_task(task_id)
            console.print("[dim]Task closed — memory state committed to git.[/dim]")
            break

        try:
            response, cost, tier, route_reason = session.execute_turn(task_id, prompt)
            last_used_tier = tier
        except PermissionError as pe:
            if "ENDGAME" in str(pe):
                if Prompt.ask("[bold yellow]Endgame mode trigger. Confirm Thinker spend? (y/n)[/bold yellow]", default="n").lower() == "y":
                    session._user_confirmed[task_id] = True
                    try:
                        response, cost, tier, route_reason = session.execute_turn(task_id, prompt)
                        last_used_tier = tier
                    except Exception as err:
                        console.print(f"[bold red]Halt: {str(err)}[/bold red]\n", err=True)
                        session.close_task(task_id)
                        break
                else:
                    console.print("[bold red]Thinker spend rejected by user. Task aborted.[/bold red]\n")
                    session.close_task(task_id)
                    break
            else:
                console.print(f"[bold red]BUDGET LIMIT:[/bold red] {str(pe)}\n", err=True)
                session.close_task(task_id)
                break
        except RuntimeError as re_err:
            console.print(f"[bold red]{str(re_err)}[/bold red]\n", err=True)
            session.close_task(task_id)
            break
        except Exception as e:
            console.print(f"[bold red]Execution Error:[/bold red] {str(e)}\n", err=True)
            continue

        console.print(f"[blue]⚙️ {route_reason}[/blue]")
        console.print(f"\n[bold green]Response ({session.client.select_model(tier, 'anthropic')}):[/bold green]")
        console.print(response)
        console.print(
            f"\n[dim]Cost: ${cost:.5f} | Task Total: ${session.ledger.get_task_spend(task_id):.5f} "
            f"| Remaining Budget: ${session.ledger.get_remaining_budget():.5f}[/dim]\n"
        )


@cli.command()
def reindex():
    """Walks the workspace and updates the codebase graph sqlite database."""
    session = CouncilSession()
    console.print("[yellow]Indexing codebase graph dependencies...[/yellow]")
    session.graph.seed_from_workspace()
    console.print("[bold green]Codebase graph reindexed successfully.[/bold green]")


@cli.command()
def stats():
    """Prints routing analytics, overhead metrics, and spend statistics."""
    from council.analytics import AnalyticsLogger
    logger = AnalyticsLogger()
    ledger = Ledger()
    stats_data = logger.get_statistics(ledger.data)
    
    table = Table(title="Council Routing Statistics", show_header=True, header_style="bold cyan")
    table.add_column("Metric", style="dim")
    table.add_column("Value", justify="right")
    
    table.add_row("Total Turns Logged", str(stats_data["total_turns_logged"]))
    table.add_row("Manual Override Count", str(stats_data["override_count"]))
    table.add_row("Failed-then-Escalated Count", str(stats_data["failed_then_escalated_count"]))
    
    avg_spend = stats_data["avg_spend_per_turn_by_route"]
    table.add_row("Avg Spend (Thinker Route)", f"${avg_spend.get('thinker', 0.0):.5f}")
    table.add_row("Avg Spend (Explorer Route)", f"${avg_spend.get('explorer', 0.0):.5f}")
    
    table.add_row("Orchestration Spent", f"${stats_data['orchestration_spent']:.5f}")
    table.add_row("Total Spent", f"${stats_data['total_spent']:.5f}")
    table.add_row("Orchestration Overhead %", f"{stats_data['orchestration_overhead_pct']:.2f}%")
    
    console.print(table)


@cli.command()
@click.option("--passing-commit", required=True, help="Last known passing git commit hash")
def rehydrate(passing_commit: str):
    """Runs rehydration quiz and triggers automated Git bisect recovery on failures."""
    import sqlite3
    session = CouncilSession()
    from council.rehydration import RehydrationTester, BisectRecovery
    
    # 1. Fetch transcripts
    conn = sqlite3.connect(session.memory.db_path)
    cursor = conn.cursor()
    cursor.execute("SELECT task_id, role, content FROM transcripts")
    rows = cursor.fetchall()
    conn.close()
    
    transcripts = [{"task_id": r[0], "role": r[1], "content": r[2]} for r in rows]
    
    tester = RehydrationTester(session.client)
    console.print("[yellow]Generating weekly rehydration quiz questions...[/yellow]")
    quiz = tester.generate_quiz(transcripts)
    
    # 2. Get current memory snapshot
    memory_snapshot = ""
    for filename in ["invariants.md", "decisions.md", "questions.md", "persona.md"]:
        path = os.path.join(session.memory.memory_dir, filename)
        if os.path.exists(path):
            with open(path, "r") as f:
                memory_snapshot += f.read() + "\n"
                
    console.print("[yellow]Running first quiz pass...[/yellow]")
    score1 = tester.score_quiz(memory_snapshot, quiz)
    console.print(f"Quiz pass 1 score: {score1 * 100:.1f}%")
    
    if score1 >= 0.8:
        console.print("[bold green]Rehydration verification passed successfully.[/bold green]")
        return
        
    # 3. Confirm failure with a second run to prevent stochastic variance triggers
    # Use a different quizzer model (e.g. OpenAI or Gemini if available, or explorer tier if only Anthropic is available)
    confirm_provider = "anthropic"
    confirm_tier = "explorer"
    if session.client.openai_key:
        confirm_provider = "openai"
        confirm_tier = "cheap"
    elif session.client.gemini_key:
        confirm_provider = "gemini"
        confirm_tier = "cheap"

    console.print(f"[yellow]Quiz failed (< 80%). Running confirmation pass with different model ({confirm_provider}/{confirm_tier})...[/yellow]")
    score2 = tester.score_quiz(memory_snapshot, quiz, provider=confirm_provider, tier=confirm_tier)
    console.print(f"Quiz pass 2 score: {score2 * 100:.1f}%")
    
    if score2 >= 0.8:
        console.print("[bold yellow]Rehydration verification passed on confirmation pass (stochastic variance).[/bold yellow]")
        return
        
    console.print("[bold red]Rehydration failure confirmed. Triggering Git Bisect recovery...[/bold red]")
    bisect = BisectRecovery(session.memory.workspace_path)
    
    bad_commit, log = bisect.bisect_and_revert(passing_commit, tester, quiz)
    console.print(f"\n[bold red]Culprit commit identified and reverted:[/bold red] {bad_commit}")
    
    # Print diff of the reverted commit
    try:
        diff = bisect.run_git(["diff", f"{bad_commit}^!", "--", ".council/memory/"])
        console.print("\n[bold cyan]Reverted Memory Diff:[/bold cyan]")
        console.print(diff)
    except Exception:
        pass


@cli.command()
@click.option("--port", default=8080, help="Port to run the dashboard server on")
def dashboard(port: int):
    """Starts the local web dashboard server and opens it in your default browser."""
    from council.dashboard_server import run_server
    try:
        run_server(port)
    except Exception as e:
        console.print(f"[bold red]Failed to start dashboard server: {str(e)}[/bold red]")


if __name__ == "__main__":
    cli()
