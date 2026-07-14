# Build Brief — Phase 4: Integration Completion & Hardening

**For:** Gemini (builder) · **Verifier:** Claude (adversarial review + tests)
**Repo:** `/Users/abhay/.gemini/antigravity/scratch/council-of-ais`
**Reference:** `council-of-ais-prd.md` (Rev. 2). Do not change files under `.council/` semantics without checking the PRD FR numbers cited below.

**Note:** Claude has already fixed Phases 1–3 defects directly in this repo (real usage metering, compactor role bug, touch-list ignore list, config-driven pricing in `models.json`, tier-based FR-25, token-secured completion gates, `CouncilSession` wiring in `app.py`). Build on the current code — do not regress these. All 14 tests pass; keep them passing.

---

## Priority 0 — Close the loop left open in Phase 3 (do these first)

### 0.1 Graph seeding on init (FR-12a)
`CodebaseGraphManager` is empty until manually populated, so in any repo with
unindexed source files EVERY task fail-safes to the Thinker (verified in smoke
test). Build `seed_from_workspace()`:
- Walk workspace source files (respect `.gitignore` + the class's IGNORE lists).
- Parse Python imports (stdlib `ast`) to build edges; other languages may use
  regex import-matching for now.
- Auto-tag nodes matching high-stakes patterns (filename/path contains: schema,
  auth, migration, config, api, models, security).
- Call `precompute_reachability(k_hops=2)` at the end.
- Wire into `CouncilSession.__init__` (seed if `nodes` table is empty) and add a
  `council reindex` CLI command.
**Accept:** in this repo, "summarize the README" routes to explorer, not thinker.

### 0.2 Verification runner (FR-15/16/17 — the missing half of escalation)
Completion gates exist but nothing holds the verify token or runs checks.
Build `council/verifier.py`:
- `VerificationRunner.create_for_task(task_id, criteria: list)` — creates the
  gate via `CompletionGate.create_gate`, stores the returned token in
  `.council/verifier_tokens.json` (git-ignored; never passed into any model
  context — that is the whole point).
- `run_checks(task_id)` — executes each criterion: shell-command criteria run
  and pass on exit 0; text criteria are judged by a CHEAP model call that only
  answers pass/fail (grading, not authoring — FR-16 compliant).
- On any failure: invoke `CleanRoomEscalator.package_and_escalate` with the
  pre-attempt snapshot AND `pre_attempt_decisions` (already a parameter — use it).
- One failure → escalate. Never two cheap retries (FR-17).
**Accept:** a task with a failing criterion escalates exactly once, clean-room,
and the executing model provably never sees the token (grep the envelope).

### 0.3 Handoff-note write path (§7.2 — memory currently never gets written)
`MemoryWrapper` can store entries but nothing writes them. At task close:
- Ask the EXECUTING model (not a cheap one) for a handoff note in the §7.3
  front-matter format (class, author_model, author_tier, task_id,
  reopen_condition for decisions, body).
- Parse and store via `validate_and_save_entry`; then `commit_task_boundary`.
- Cheap-tier executors write ONLY progress/fact entries (FR-25 will reject the
  rest — rely on it, don't pre-filter).
**Accept:** after an interactive session closes, `.council/memory/` contains a
valid entry and `git log` shows the tagged task commit.

### 0.4 Decision-accumulation counter (FR-13 — currently hardcoded 0)
- Count unconsolidated `class: decision` entries authored by explorer-tier
  models in `.council/memory/`; pass the real number into `GateCascade.route`.
- After a Thinker consolidation pass, mark entries consolidated (front-matter
  field), resetting the count.
**Accept:** 5 explorer decisions force the next route to thinker.

### 0.5 Write-lockout enforcement (FR-23 — lock exists, nothing honors it)
`BisectRecovery.acquire_lock` creates the lock file, but `MemoryWrapper` writes
ignore it. Check `is_locked()` inside `validate_and_save_entry` and
`commit_task_boundary`; raise `PermissionError` with the lock reason.
**Accept:** with a lock file present, all memory writes are rejected.

---

## Priority 1 — PRD Phase 4 features

### 1.1 Endgame mode (FR-8)
In `Ledger.check_constraints`: below $15 remaining, any thinker-tier spend
returns `(False, "ENDGAME: confirm required — est $X, $Y left")` unless the
call passes `user_confirmed=True`. `app.py` catches this and prompts the user.

### 1.2 Routing analytics + flywheel labels (§10 / Upgrade 1 cold-start)
`council/analytics.py` writing `.council/routing_log.jsonl`, one line per turn:
`{task_id, prompt_features, gate_fired, tier, cost, override_used,
verification_result}`. Overrides and verification failures are the training
labels for the future local classifier — log them from day one.
Add `council stats`: spend per completed task by route, misroute proxies
(override count, failed-then-escalated count), orchestration overhead %.

### 1.3 Rehydration automation (FR-23)
`council rehydrate` command: generate quiz from SQLite transcripts, score
against current memory snapshot, on confirmed failure (two runs, different
scorer) run `BisectRecovery.bisect_and_revert`. Print the diff of the reverted
commit for one-click-style review.

---

## Rules of engagement
1. Never mark a task complete while any test fails. Run `python3 -m unittest discover tests` before claiming done.
2. Every new module gets tests, including at least one ADVERSARIAL test (wrong token, locked writes, cheap-author rejection — not just happy path).
3. No new dependencies without noting why in the PR/walkthrough.
4. Model IDs and prices go in `models.json`, never in code.
5. Update `walkthrough.md` with what was built + test output, as before.

## Verification protocol (what Claude will check)
- Full test suite + the acceptance criteria above, executed, not assumed.
- Adversarial pass: attempt to bypass FR-25, check off a gate without the token,
  write memory while locked, route a schema-touching task cheap.
- Metering audit: recorded costs vs. models.json math.
- A cold-resume test: kill a session, resume from memory only, check for loss.
