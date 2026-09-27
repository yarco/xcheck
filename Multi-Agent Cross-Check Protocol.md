---
aliases: [Multi-Agent Cross-Check, Multi-Agent Cross Check Protocol, multi agent cross check, X-Check]
tags: [multi-agent, cross-check, claude-code, codex, grok, agy, herdr]
---

# Multi-Agent Cross-Check Protocol

X-Check fans a fixed review packet out to independent peer harnesses, collects one answer per lane, and leaves judgment to the orchestrating model. It has two placements: headless lanes and visible Herdr panes. The living contract is this note; deterministic mechanics belong in `bin/xcheck` and `xcheck/`; roster choice, diagnosis, and adjudication belong to the orchestrator.

| Owner | Responsibility |
|---|---|
| This note | packet contract, invariants, harness facts, limits, merge rules |
| `bin/xcheck` + `xcheck/` | flags, process groups, deadlines, capacity, locks, placement, parsing, validation, status |
| Orchestrator | choose peers, read failures, verify premises, adjudicate, write `CONSENSUS.md` |

The code never decides whether a finding is worthwhile. The model never improvises process control.

## Run it

```bash
xcheck doctor
xcheck run --base /tmp/agent-offload/<slug> --orch claude
```

`xcheck` on PATH is the read-only snapshot at `~/.local/bin/xcheck`, installed from this repo with `bin/xcheck install`. `doctor` checks its source digest, supported flags, and model pins. Codex's allow rule admits only the absolute installed path followed by `run`, `wait`, `status`, or `doctor`; use that exact command without `cd`, a pipe, redirect, or `NAME=value` prefix. Codex orchestrators always use v2.

1. Create `BASE` under `/tmp/agent-offload/<slug>/` with `PACKET.md`, `PROMPT.md`, and `evidence/`. Record the base and roster in the task tracker.
2. Announce the roster and why the orchestrator's own harness is absent.
3. Run one blocking command. If the host tool cannot hold the cap, use `--detach`, then one `xcheck wait --base BASE --timeout S`; exit 3 means still running. This keeps the orchestrator out of the work loop.
4. Read every `RESULT-*` and `STATUS.json`, including invalid lanes. Tell the operator every non-`done` lane with its cause and remedy.
5. Verify premises against the real tree, adjudicate, and write `CONSENSUS.md` in `BASE`.

`--mode auto|headless|herdr` selects placement; `--view stream|tui` selects the harness path (default is stream headless, tui in Herdr). `--view` is retained as the small seam that exercises the app path in headless tests and controlled smokes. `--web live` is the default; `--web packet` instructs peers not to fetch web sources but is not isolation for MCP-capable lanes. `--idle` overrides the view default (900 seconds for stream, 180 for apps); `--cap` defaults to 3600 seconds. `--force-lane NAME` overrides capacity for a selected lane; `--skip-capacity` is for controlled smokes. Invalid options are refused before a pane, model, or launch marker is created. A fresh `BASE` is required for a rerun.

Exit codes: 0 all lanes settled; 2 refused before spend; 3 wait timed out while work continues; 130 supervisor interrupted.

| Host | How to hold the call |
|---|---|
| Claude Code | background `xcheck run`; return on completion |
| Codex | absolute installed `xcheck run --detach`, then absolute `xcheck wait --timeout` |
| Grok | Herdr mode, background command, one long wait |
| opencode/pi/other | detach and wait within the shell call's cap |

## Roster

The default roster is pinned in `xcheck/adapters.py`: Codex `gpt-6-sol`, Claude `claude-opus-5-5`, Grok `grok-4.7`, Gemini Pro and Flash, and Agy Claude Sonnet. The orchestrator's kind is removed automatically; `--orch agy` removes every Agy lane. Agy Opus Thinking is opt-in; GPT-OSS 120B is never used because its agent loop aborts. Pro and Flash are different evidence, and same-family lanes are correlated. After roughly three strong, independent peers, extra lanes usually have diminishing returns; add one only for a distinct capability or failure hypothesis. `done` certifies the output contract, not review quality.

## Packet contract

```text
BASE/
  PACKET.md        provenance first; exactly one `Proof target: <BASE-relative path>`
  PROMPT.md        answer schema; exactly one `RESULT_REQUIRE: <KEYWORD>`
  evidence/        whole staged files, with repository-relative paths
```

The proof target must be `PACKET.md`, `PROMPT.md`, or a file under `evidence/`; it must be UTF-8, non-empty, inside `BASE`, and not a symlink. Its last non-empty line is an unguessable token. Do not edit the packet after launch. Stage whole files and preserve paths; state any deliberately omitted files and the provenance of every claim. A substring search is not an API or call-set proof.

The lane message says the owner convened the review, to read `PACKET.md` then `PROMPT.md`, and to work from the staged directory. Connected MCP tools are available to every peer; external actions remain forbidden by instruction. Web is available in live mode. Grok may use Bash and disposable workspace scratch; other lanes use read tools plus the answer channel. Stream lanes print their answer as the final message. TUI lanes write only `ANSWER.md` in one complete write. The operator's global rules can still reach a peer; the lane message scopes them to this review.

Answer schema:

1. `VERDICT:` with a decision and its key reason, at most 80 words.
2. Findings: severity, evidence, root cause, mechanism and premises, frequency and cost, smallest repair, correctness contract, a regression check that fails while the defect exists, and confidence.
3. What to delete and why it is safe.
4. Alternatives, if useful.
5. One-line recommendation.
6. `PROOF:` containing the proof target's final line verbatim.

The final non-empty line is exactly `RESULT_COMPLETE` or `RESULT_INVALID`. The validator requires the requested heading, matching proof, and substantive review text. Invalid answers remain available as leads.

## Run artifacts

`BASE/run.json` freezes the roster, contract, view, budgets, and pane IDs. Each lane has `message.txt`, `staged`, `exit.json`, `pids.json`, a stream or screen log, stderr, and a harness debug log when the CLI supports one. `RESULT-<lane>.md` is the captured answer. `STATUS.json` and `STATUS.md` contain one row per selected lane. Stable workspaces are `ROOT/.lanes/<lane>` and are restaged only after pool admission; the `staged` stamp prevents salvaging an answer left by an earlier run. Keep all run artifacts in `/tmp`; do not copy them into the vault.

## Status and operator actions

`done` means a valid answer. `invalid` means an answer exists but violates the schema; read it and report the defect. `failed` means no answer; use the structured error, first stderr line, and tail. `timeout` means an idle or cap bound ended the lane; inspect saved stream or screen. `skipped` means reserve, auth, or unknown capacity; never relaunch a quota/auth death in the same run. `interrupted` means the supervisor was signalled; reattach or use a fresh base.

A lane that dies within seconds with a usage message usually has flag drift: read stderr line one, run `xcheck doctor`, and compare the installed CLI help before blaming quota or the model. The only answer channels are a stream final message and `ANSWER.md`; ad hoc scratch is forensic evidence, not a result.

## Modes and placement

Headless lanes are child processes of the supervisor. Herdr uses the caller pane only to split a right-hand column (minimum 176×14), then runs the same lane runner in each returned pane with `exec`. The runner owns a pty for the harness's real TUI, forwards bytes and keystrokes, and waits for a stable terminal marker in `ANSWER.md`. Herdr closes a pane when its `exec` process exits; the runner lingers three seconds so the final line is readable. A control failure after a split but before `pane run` can leave an empty pane; close that pane by hand. No pane ownership or cleanup bookkeeping is needed.

Every runner writes `exit.json` once, holds the pool lock while the harness can spend, reaps its process group, cancels its kernel alarm, and returns a status. The runner's alarm is `2*cap+300` seconds: admission and spend each have a cap, with room for capacity and shutdown. A signal during Herdr linger is caught. The supervisor has no watchdog; this bound also works after supervisor loss. `wait` reattaches if the supervisor died. A detached descendant that closes or escapes its inherited descriptor and outlives both runner and harness is outside scope; a leaked holder can keep a pool busy. Diagnose with `lsof +D /tmp/agent-offload/.pools`.

## Capacity and locking

Each lane probes its own pool immediately before spending, after admission. Codex, Claude, Grok, and Agy readings are keyless subscription snapshots; reserve is 10%, while Agy's family reserve is `5 + 5n` percent for `n` lanes, capped at 90. A healthy family reading cannot rule out a per-model cap. Capacity refusal is `skipped`; runtime quota/auth errors are structured failures and never retried.

A pool has a short exclusive `gate`, a `busy` file, and an `owner` tag under `ROOT/.pools/<pool>/`. One run owns a pool at a time; lanes of that run share it, disjoint pools proceed in parallel. The runner takes a kernel `flock`, and the harness inherits its descriptor. The gate serializes admission and the same deadline bounds waiting for gate and pool. Owner text is metadata, not liveness. Never replace a live lock pathname. This protects capacity snapshots and the stable, exact-path lane workspace. It does not claim adversarial filesystem isolation or descendant survival.

Runner and supervisor liveness are exclusive locks on their `alive` files, released by the kernel on crash. PIDs are records for diagnosis only; no process-table scan is required to prove liveness. One `BASE/.launched` marker prevents duplicate fan-out and is created after packet and option checks.

## Harness facts

`xcheck doctor` validates the flags it constructs. Recheck after CLI upgrades; a successful fake stream test is not a live tool invocation.

- **Codex:** `codex exec --ephemeral -s read-only -C WORK -m MODEL -c model_reasoning_effort=EFFORT -c web_search=live|disabled -c check_for_update_on_startup=false --json -o FINAL -`. Connected MCP servers come from user config; no `--ignore-user-config`. TUI web uses `--search`. Stable lane workspaces under both `/tmp` spellings are pre-trusted; no click is allowed.
- **Claude:** `claude -p --restricted --permission-mode dontAsk --permission-prompts none --tools ... --allowedTools ...,mcp__* --add-dir WORK --no-session-persistence --model MODEL --effort EFFORT --output-format stream-json --include-partial-messages --debug-file LOG`. Restricted mode still loads MCP; `--strict-mcp-config` is intentionally absent. A live Claude stream smoke produced a model answer without an approval prompt; its first result exposed and is now covered by the formatted two-line PROOF regression test. Claude denial and MCP invocation remain separate, unverified cases.
- **Grok:** `--sandbox read-only --always-approve`, Bash and connected MCP enabled, memory and updater disabled by environment. Hooks work in read-only; strict breaks an operator hook. Tmp writes, including another lane's tmp tree, remain an accepted residual.
- **Agy:** always the interactive app, including headless placement: `--sandbox --dangerously-skip-permissions --mode accept-edits`. It reads the packet through its own tools; the old stdin bundle and 192 KB cap are gone. Print mode can lose the answer after a denied tool. Agy's connected MCP set is harness-specific and has no per-run allowlist; trust and survey settings are prepared automatically.
- **Environment:** scrub orchestrator identity (`CLAUDE*`, `HERDR_*`, `CODEX_COMPANION_*`, `CODEX_SANDBOX*`), `GROK_SANDBOX`, and every `*_API_KEY`; preserve harmless user preferences. Never relocate auth homes to fake isolation.
- **Forensics:** stream/screen logs, per-lane harness logs, stderr tails, `pids.json`, and `exit.json` stay in `BASE`; artifacts stay in `/tmp`.

## Invariants

| ID | Rule |
|---|---|
| I1 | Peers differ from the orchestrator and receive the same immutable packet. |
| I2 | Whole-file evidence, preserved paths, and an exact proof token support review. |
| I3 | Connected MCP tools are exposed; no manual approval is part of a run. |
| I4 | Each runner bounds itself with idle/cap loops and a kernel alarm at `2*cap+300`. |
| I5 | Every selected lane has one status row; every non-done lane is reported. |
| I6 | Capacity is read under pool admission; no automatic retry. |
| I7 | Stream final text and stable `ANSWER.md` are the only answers. |
| I8 | One launch per `BASE`; reattachment never relaunches. |
| I9 | Kernel locks, not PIDs, prove live holders. |
| I10 | One runner and one validation path serve headless and Herdr placements. |
| I11 | Lane names encode exact harness/model/effort provenance. |
| I12 | The orchestrator makes no calls while lanes work. |

## Current limits

- Scope is one attended operator on one Mac; reboot-mid-run, adversarial peers, and unattended multi-hour recovery are outside the contract.
- A static account-verification splash can hit the 180-second app idle. The last screen is retained; no prompt is resent.
- A real Herdr control failure after split may leave an empty pane.
- MCP external actions and packet-mode web restrictions are instruction-level, not sandbox-level.
- Grok can write in `/tmp`, including another lane's workspace.
- Real quota/auth error fixtures and Claude denial cases should be captured when they occur; invalid-model fixtures prove parser shape only.
- A descendant that escapes the inherited lock can outlive the run; a leaked descriptor keeps the pool busy until the holder exits.

## Deliberately absent

| Legacy mechanism | Why it is absent |
|---|---|
| Credit/auth log classifiers and retries | structured errors plus no retry are deterministic and safer |
| Late harvest, scouting, provisional labels, notices files | one declared answer channel and operator judgment are enough |
| Spender claims, PGID fallback, boot identity, daemon reaper | v2 owns the spending runner and uses kernel lifetime |
| Global lock and live-lane scans | per-pool admission preserves parallelism and capacity meaning |
| Supervisor watchdog and pane ownership cleanup | runner alarm and Herdr `exec` lifecycle settle the lane |
| Herdr agent readiness, splash classifiers, prompt resend | prompt is argv in a pty; static screens become bounded timeouts |
| Agy stdin bundle, 192 KB cap, read-depth probe | file transport reaches large packets; the probe was gameable |
| Host router, note read-map, wait metrics | one note and structural blocking are simpler |
| Perplexity lane and `~/.agents` runtime | not a default peer; installed runtime lives under `~/.local` |
| Claude `--mcp-config` | restricted mode loads connected MCP without the redundant file |

## Review discipline

The first round is independent brainstorming. Only after that do peers read the orchestrator plan and compare it. A follow-up round stages the delta and the settled decision lineage, rather than replaying historical run debris. Ask reviewers what they would delete, which in-scope failure is unhandled, and which Legacy insight was lost. Three strong independent peers are normally enough; use another lane only for a distinct evidence source.

## Merge

1. Read `STATUS.json` and every result immediately before merging.
2. Check each finding's premises against the real files; absence and wrong-path claims require opening the tree.
3. Treat same-family responses as correlated; never tally votes.
4. Separate diagnosis from repair. The verification must fail while the defect exists.
5. Write `CONSENSUS.md` with one decision, confirmed/rejected/uncertain findings, lane provenance, selected repairs, and unusable lanes.
6. Review applied fixes in a fresh round scoped to the delta. Stop when no actionable finding remains.

A finding earns code only when its failure has happened or is likely within scope (one operator, one Mac, attended runs, concurrent orchestrators). An in-scope finding gets a tested repair. A real but out-of-scope issue gets one line in Current limits. Everything else is rejected in `CONSENSUS.md`. Do not add prose to pre-empt a reviewer. A change should keep or reduce total size unless it adds an operator-requested capability.

## Repository

Run `python3 -m unittest discover -s tests` and `git diff --check`. The suite uses stdlib plus unguarded Hypothesis, fake harnesses, captured streams, and property laws for pure contracts. Tests isolate `XCHECK_ROOT`, Codex, Agy, and capacity configs. Dropbox can reset new executable files to mode 600; restore `755` for `bin/xcheck`, `bin/agent-capacity`, and `tests/fake/*`. After changing code, run `bin/xcheck install` and `xcheck doctor`.

Legacy is read-only at `../x-check-old/`; history is `git log`. Durable requirements, invariants, and limits live here, not in TODOs or decision ledgers.
