# Council of AIs

> Route each task to the cheapest model tier that can handle it safely, keep context alive across sessions, and always know what you have spent.

Council of AIs is a task-oriented multi-model orchestration, budget-control, and memory-rehydration engine. It classifies each request by stakes, blast radius, token volume, and ambiguity, routes it to an appropriate model tier, and enforces spend limits in code rather than leaving them to model discretion. A persistent memory layer carries invariants and decisions between sessions so that switching models does not mean starting over.

[![License](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python Version](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/downloads/)
[![Unit Tests](https://img.shields.io/badge/tests-47%20passed-green)](#testing)

**Status:** v0.1.0, early. The engine runs, the test suite passes, and it is in daily use on one codebase. It has no published routing benchmark yet, no PyPI release, and no external users. See [Roadmap](#roadmap) for what is next and [Limitations](#security--honest-limitations) for what it does not do.

[Problem](#the-problem) · [Example session](#example-session) · [Features](#features) · [Quick start](#quick-start) · [Architecture](#system-architecture) · [CLI](#cli-reference) · [Budget controls](#budget-controls) · [Memory](#memory-layer-graphify) · [Benchmarks](#benchmarks) · [Limitations](#security--honest-limitations) · [Testing](#testing) · [Roadmap](#roadmap)

---

## The problem

Agentic coding work has an awkward cost shape. Most turns in a long task are routine: rename this, write the test, apply the pattern already decided three turns ago. A small number are consequential: choose the architecture, write the contract the rest of the work implements, review the diff that touches forty files.

Running a premium reasoning model on all of it is expensive. Switching down to a cheaper model by hand is worse, because the cheap model has no memory of the invariants and decisions the expensive one established, and it will quietly contradict them. So the usual outcome is to pay premium rates for routine work, or to accept drift.

Council of AIs treats **models as commodities and context as capital**. Routing decides which tier sees a given turn. The memory layer makes sure the cheap tier inherits the constraints rather than re-deriving them, and is programmatically forbidden from rewriting them. A transactional ledger keeps the whole thing inside a budget you set, including when several branches run at once.

---

## Example session

A short task run end to end. Output is abbreviated.

```console
$ python3 -m council.app start-task "Add rate limiting to the /ingest endpoint" \
    --criteria "cmd:python3 -m unittest tests.test_ingest, Returns 429 once the cap is exceeded"

Task created: t_4f2a91
Criteria registered: 1 shell verifier, 1 checklist item
Budget remaining: $61.4820   Reserve floor: $10.00
```

The first turn touches the request path and the config schema, so the gate cascade sends it up:

```console
$ python3 -m council.app run-turn t_4f2a91 "Design the limiter: where it sits, what it stores, how the cap is configured."

[Leader] Gate 2 (graph touch: 6 modules) -> THINKER
[Thinker Response (Opus-tier)]
Contract written to .council/tasks/t_4f2a91/contract.md
  - Token bucket in middleware, before auth
  - Redis-backed counter, per-key window from config
  - 429 with Retry-After; no change to handler signatures

Turn cost: $0.1840  | Remaining Budget: $61.2980
```

The implementation turn is bounded by that contract, so it routes down:

```console
$ python3 -m council.app run-turn t_4f2a91 "Implement the contract. Update tests."

[Leader] Gate 3 (volume, contract-bounded) -> EXPLORER
[Explorer Response (Sonnet/Flash-tier)]
Wrote middleware/ratelimit.py, tests/test_ingest.py
Verifier: cmd:python3 -m unittest tests.test_ingest -> PASS

Turn cost: $0.0213  | Remaining Budget: $61.2767
```

And the receipt:

```console
$ python3 -m council.app ledger

Budget Remaining:    $61.2767
Reserve Floor Limit: $10.00 (locked for Thinker)

              Task Spending History
 Task ID   Spend Amount   Turn Count
 t_4f2a91  $0.2053        2
```

Two turns, one of them on the expensive tier because it earned it. The contract the Thinker wrote is now an entry in `decisions.md`, which the Explorer inherits on every later turn of this task and cannot overwrite.

---

## Features

**Multi-tier dynamic routing**

- **Bypass gate.** Trivial queries matched against regex patterns and explicit bypass commands, routed without consulting a router model at all.
- **Leader gate cascade.** Four gates for everything else: stakes, codebase import-dependency touch-list, token volume, and query ambiguity.
- **Thinker tier.** Premium reasoning models acting as supervisor, reviewer, and contract writer.
- **Explorer tier.** Mid-tier models running implementation, code execution, and tool calls.

Tier-to-model mappings and prices live in a machine-local `models.json`; defaults ship in `council/ledger.py`. Custom thinking levels are supported for models that expose reasoning-effort controls.

**Prompt-cache envelope.** `CacheEnvelope` splits context into four layers: system invariants that never expire, architectural decisions with explicit reopen conditions, the active task canvas, and compacted volatile history.

**Council chamber collaboration.** Bounded working sessions where the Thinker drafts a contract and the Explorer implements it. The Explorer may ask at most one clarifying question. Review rounds are hard-capped at three to prevent model-to-model agreement loops. If the loop fails to converge, a clean-room escalation hands the task to the Thinker alone.

**Parallel solution trees.** Opt-in concurrent Explorer branches (`mode="tree"`) across providers. The Thinker evaluates candidates anonymised as Branch A/B/C so provider identity cannot bias the choice, then selects one or drafts merge instructions. If a branch fails its budget check the session degrades gracefully and continues with the rest.

**Native MCP tool support.** Standard stdio JSON-RPC client for Model Context Protocol servers, bounded to five tool calls per turn. All tool output is treated as untrusted and quarantined inside `[QUOTED-TOOL-OUTPUT]` delimiters.

**Sandboxed execution.** Verifiers and model-generated code run in Docker with `network=none`, memory and CPU limits, and read-only mounts. Falls back to subprocess execution with an explicit warning when Docker is unavailable.

**Web dashboard.** Local server showing budget remaining, cost metrics, the active model map, and side-by-side parallel tree feeds.

---

## Quick start

### 1. Install

```bash
git clone https://github.com/ProductGurusAI/Council-of-AIs.git
cd Council-of-AIs
pip install -e .
```

Python 3.10 or newer.

### 2. Configure keys and budget

```bash
cp .env.example .env
```

```ini
ANTHROPIC_API_KEY=your_key_here
OPENAI_API_KEY=your_key_here
GEMINI_API_KEY=your_key_here

TOTAL_BUDGET=75.00
RESERVE_FLOOR=10.00
PER_TASK_CAP=3.00
```

Keys stay in your local environment. Nothing is sent anywhere except to the model providers you configure.

### 3. Docker (optional)

If the Docker daemon is running, verifiers execute in an isolated container automatically. If it is not, execution falls back to a subprocess and logs a warning. See [Limitations](#security--honest-limitations) before running untrusted code without Docker.

### 4. Run

```bash
python3 -m council.app                # interactive workbench
python3 -m council.app dashboard      # http://localhost:8080
```

---

## System architecture

```mermaid
graph TD
    User(["User Task Input"]) --> Bypass{"Bypass Gate"}
    Bypass -- "Trivial Matching" --> Cheap["Local Subprocess / Cheap Model"]
    Bypass -- "Non-Trivial" --> Leader["Leader: Gate Cascade"]

    Leader -->|Gate 1 & 2: Stakes / Graph Touch| Thinker["Thinker: Premium Model"]
    Leader -->|Gate 3: Token Volume Cap| Explorer["Explorer: Mid-Tier Builder"]
    Leader -->|Gate 4: Ambiguity Threshold| Explorer

    Explorer -->|Defect / Failure| Escalation["Clean-Room Escalation"] --> Thinker

    Thinker -->|Consolidated Output| Graphify["Graphify: Memory Compression"]
    Explorer -->|Handoff Note| Graphify

    Graphify -->|Write Commit| Memory[("SQLite Transcripts / Markdown")]

    subgraph Controls
        Ledger[("SQLite Atomic Reservations")]
    end

    Thinker -.->|Reserve & Commit| Ledger
    Explorer -.->|Reserve & Commit| Ledger
```

| Component | Role |
|---|---|
| **Leader** | Fast gateway analysing token counts, bypass patterns, and the codebase dependency graph |
| **Thinker** | Premium reasoning model: task mapping, contracts, review, escalated defects |
| **Explorer** | Standard model: code generation, test runs, MCP tool calls |
| **Graphify** | Compacts raw transcripts into structured markdown memory |
| **Ledger** | Transactional database tracking and capping spend |

---

## CLI reference

Run as `python3 -m council.app <command>`.

| Command | What it does |
|---|---|
| `interactive` | Task workbench console with live cost tracking. `!think <prompt>` and `!cheap <prompt>` override routing. |
| `start-task "<goal>" [--criteria "<items>"]` | Initialises a task and its verification gate. Prefix a criterion with `cmd:` to run it as a shell verifier. |
| `run-turn <task-id> "<prompt>" [--provider <name>]` | Executes one turn under budget control. Providers: `anthropic`, `openai`, `gemini`. |
| `ledger` | Prepaid pool remaining, total spend, reserve floor, per-task transaction table. |
| `dashboard [--port <port>]` | Local HTTP dashboard, default port 8080. |
| `reindex` | Rebuilds the SQLite dependency graph at `.council/graph.db`. |
| `stats` | Turn analytics, failed-then-escalated counts, average routing cost, orchestration overhead. |
| `rehydrate --passing-commit <hash>` | Generates a recall quiz from archived transcripts and triggers git-bisect recovery on failure. |

---

## Budget controls

Every constraint below is enforced in Python. None of it is delegated to a model.

- **Reserve floor ($10.00 default).** The last slice of the pool is locked and spendable only on Thinker clean-room escalations, so a failing task can always be rescued.
- **Per-task cap ($3.00 default).** A task crossing the cap pauses and waits for explicit confirmation.
- **Endgame mode ($15.00 default).** Below this threshold, every Thinker call requires confirmation.
- **Runaway loop protection.** Execution halts if a task revisits the same state three times without progress.
- **Atomic reservations.** Ledger writes and spend evaluations run inside SQLite `BEGIN IMMEDIATE` transactions, so parallel trees reserve budget without double-spending. Reservations older than ten minutes expire automatically.

---

## Memory layer (Graphify)

Memory is a pyramid stored in `.council/memory/`:

1. **Invariants** (`invariants.md`) — hard constraints and touch-lists. Verbatim, never expires.
2. **Decisions** (`decisions.md`) — rationale plus explicit reopen conditions.
3. **Open questions** (`questions.md`) — known unknowns blocking execution.
4. **Progress narrative** — Mermaid state maps of task checkpoints.
5. **Evidence pointers** — transcripts in SQLite (`transcripts.db`) with stored embeddings and cosine-similarity lookup for recall.

### Authorship rule

Cheap, leader, and bypass-tier models are barred from authoring invariants or decisions. `MemoryWrapper` raises `PermissionError` on the attempt. This is the guard that makes downward routing safe: a cheap model can record progress, but it cannot quietly redefine a constraint the expensive model established.

### Rehydration and recovery

On a weekly schedule or manual trigger, the rehydration tester builds a ten-question quiz from past transcripts and scores recall. Below 80%, a confirmation pass runs; a confirmed failure triggers git-bisect recovery back to the last passing state and writes `write_lockout.lock` to block further memory writes until a human resolves it.

---

## Benchmarks

`council/council_bench.py` contains the harness and a labelled dataset of routine tasks used to check routing accuracy and sandboxed execution of generated code.

**There is no published end-to-end benchmark yet, and the README will not claim one until there is.** The comparison that matters is a fixed task set run through the routed pipeline against a single-tier baseline, scored on completion and correctness rather than on tokens saved, with the raw data committed so anyone can re-run it. That is the next substantial piece of work on this repo.

Cost figures the ledger reports are computed from token counts and the prices in your `models.json`. They are estimates and do not reflect taxes, provider discounts, or billing adjustments.

---

## Security & honest limitations

- **Local keys.** API keys live in your `.env` and local environment. No telemetry.
- **Untrusted tool quarantine.** MCP stdio output is treated as hostile text, wrapped in delimiters, and never routed into the bypass lane or the gate cascade.
- **Subprocess fallback is not a sandbox.** Docker enforces network and volume constraints. Without it, generated code runs in a local subprocess, and code execution or command injection can reach the host. Do not run untrusted code without Docker.
- **Machine-local `models.json`.** Pricing and tier maps do not travel with the repository. Cloning onto a new machine means reconfiguring them.
- **Model IDs and prices go stale.** Entries in `council/ledger.py` carry a `verified` flag. Check your own provider pricing before trusting a cost figure.
- **Estimate-based ledger.** See [Benchmarks](#benchmarks).
- **One user so far.** This has been exercised hard on a single codebase by a single developer. Expect rough edges in setups unlike that one, and please open an issue when you find them.

---

## Testing

```bash
python3 -m unittest discover tests
```

The suite covers JSON migrations, thread-safe reservations, parallel tree degradation, Docker isolation, and rehydration logic.

```
Ran 47 tests in 8.971s
OK (skipped=2)
```

The two skips are the Docker network and volume isolation checks; start the Docker daemon to run them.

---

## Roadmap

- Published routing benchmark with committed raw data and a re-runnable harness
- Tagged release on PyPI
- Documentation beyond this README, including the gate-tuning guide
- Verified pricing table refreshed against provider docs
- Provider plug-in interface so new backends do not require editing the ledger

---

## Contributing

Issues and pull requests are welcome, particularly around gate tuning, provider support, and anything that makes the benchmark more honest. Run the test suite before opening a PR.

---

## License

MIT. See [LICENSE](LICENSE).

---

## About

Council of AIs was built by [ProductGurus](https://productgurus.ai), a product research and build studio in Vancouver, BC. It is one of seven products taken from first interview to release.

**Case study:** [How Council of AIs was built](https://productgurus.ai/projects/council-of-ais) — the routing decisions, the cost gap between model tiers that motivated the budget engine, and the parts that are still unresolved.

**Other work:** [productgurus.ai/work](https://productgurus.ai/work)
