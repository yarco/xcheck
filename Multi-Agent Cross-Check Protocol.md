---
aliases: [Multi-Agent Cross-Check, Multi-Agent Cross Check Protocol, multi agent cross check, X-Check]
tags: [multi-agent, cross-check, claude-code, codex, grok, agy, herdr]
---

# Multi-Agent Cross-Check Protocol

X-Check fans one immutable review packet out to **other** agent CLIs (Codex, Claude Code, Grok, Agy) as real processes, collects one answer file per lane, and leaves judgment to the orchestrating model. The same lane runner has two placements: **headless** (child processes) and **Herdr** (one visible pane per lane, right of the orchestrator, showing each harness's real app).

| Owner | Holds |
|---|---|
| This note | packet contract, invariants, harness facts and why they hold, limits, merge rules |
| `bin/xcheck` + `xcheck/` | everything with one mechanical right answer: exact argv, process groups, deadlines, capacity, locks, placement, stream parsing, validation, status |
| The orchestrator | judgment: roster, packet, diagnosing a failed lane and telling the operator, premise checks, adjudication, `CONSENSUS.md` |

The code never judges a finding and never retries; the model never improvises process control. Exact flags live only in `xcheck/adapters.py`; this note records why each non-obvious one is there.

Scope: one operator, one macOS machine, attended runs, possibly several orchestrator sessions running cross-checks at once. Reboot mid-run, adversarial peers, and unattended multi-hour recovery are outside the contract.

## Run it

```bash
xcheck doctor                                              # installed copy, flags, model pins
xcheck run --base /tmp/agent-offload/<slug> --orch claude  # default roster minus the orchestrator's harness
```

`xcheck` on PATH is `~/.local/bin/xcheck`, a read-only, content-addressed copy that `bin/xcheck install` publishes from this repo. `doctor` compares it with the source digest even when invoked through the copy. Reinstall after changing `bin/` or `xcheck/`.

1. **Stage** `BASE=/tmp/agent-offload/<slug>/` (Packet below). Stage and launch before loading unrelated material: every later return re-sends the whole context.
2. **Record** BASE, roster and the exact command in the existing task tracker or chat before spending. `run.json` persists the settings; write no separate handoff for a run.
3. **Announce**: `Fan-out: N peers → <lanes>. Not peers: <harness> (<why>)`.
4. **Run** without spending turns: one call that blocks until every lane has ended and prints only the status table. Do not read panes, files or logs while it runs.

   | Orchestrator | Form |
   |---|---|
   | Claude Code | `xcheck run …` as a background shell job; its completion notice is the one return |
   | Codex | absolute `~/.local/bin/xcheck run … --detach`, then absolute `~/.local/bin/xcheck wait --base B --timeout S` under the call's timeout. The allow rule written by `install` matches only that absolute path followed by `run`, `wait`, `status`, `doctor` or `capacity`: no `cd … &&`, pipe, redirect, `NAME=value` prefix, or trailing punctuation. It runs the command outside Codex's sandbox with no approval; its target is under `~/.local`, which that sandbox cannot write. Never point it at the vault source |
   | Grok | Herdr mode only (its sandbox covers its whole process tree, so `run` refuses headless). Background `xcheck run`, then one `get_command_or_subagent_output` with `timeout_ms` up to 3 600 000 |
   | opencode | `--detach`, then `wait --timeout S` with the shell call's `timeout` above S. Its shell tool has no background parameter and defaults to 120 s; the detached supervisor is in its own session and survives the call. `--orch opencode` drops opencode lanes |
   | pi | `--detach` + `wait --timeout <call cap>`; never pi's own `delegate`/`subagent` tools as peers (they start pi) |
   | Agy | allowed; `--orch agy` drops every Agy lane |
   | anything else | `--orch other`; one blocking wait or `--detach` + `wait --timeout S`. Never claim a different supported harness to bypass same-harness exclusion |

   `wait --timeout S` exits 3 while lanes still run and prints the current table; each extra return is one short message.
5. **Report** every non-`done` lane in the next chat message from `STATUS.json` (`reason`, `stderr_tail`): `<lane> <status> — <cause in plain words>. Fix: <remedy>. Continuing with <others>.`
6. **Merge** (below) into `BASE/CONSENSUS.md`.

`run` options: `--lanes kind:model[:effort],…` replaces the roster (Agy takes no effort, opencode's is an optional `--variant`, every other kind needs one); `--add LANE` appends an opt-in lane to it. `--mode auto|headless|herdr` (auto = Herdr when available). `--view stream|tui` defaults from placement (stream headless, tui in Herdr); it exists so tests and controlled smokes can drive the app path without Herdr. `--web live` (default: peers may search and must cite what they relied on) or `packet` (built-in web tools off and peers told not to fetch; connected MCP tools remain, so this is not isolation). `--idle` overrides the view's inactivity limit (180 s for apps, 900 s for streams). `--cap` is each lane's spend budget (3600 s); a lane queued behind another run waits up to `--cap` in its own process, then ends `skipped`. `--force-lane NAME` launches a selected lane past a capacity refusal, only on the operator's say-so and announced in chat. `--skip-capacity` is for controlled smokes. `--detach` returns once launched.

Invalid options (no peers, non-positive budgets, a forced lane not in the roster, a same-kind peer) are refused before any pane, model or launch marker exists. Other commands: `xcheck wait --base B [--timeout S]` (blocks; also takes over a dead supervisor and finishes its job), `xcheck status --base B` (render from files).

Exit codes: 0 every lane settled (read the table); 2 refused before any spend; 3 `wait` timed out while lanes run; 130 supervisor interrupted.

## Roster

| Lane | Pool |
|---|---|
| `codex-gpt-6-sol-high` | codex |
| `claude-opus-5-5-high` | claude |
| `grok-4.7-xhigh` | grok |
| `agy-gemini-3.1-pro-high` | agy-gemini |
| `agy-gemini-3.8-flash-high` | agy-gemini |
| `agy-<third-party-model>` | agy-3p |

The orchestrator's harness is removed automatically. A lane name is its provenance (`<harness>-<exact model>[-<effort>]`) and names exactly what is launched. Pins live in `xcheck/adapters.py`; `doctor` confirms the Grok and Agy pins appear in `grok models` / `agy models`. Refresh pins deliberately after checking the installed CLIs: when `agy models` lists a newer Flash, replace that one row and leave Pro and Sonnet alone. Claude pins use the versioned id (`claude-opus-5-5`, not `opus-5-5`); capacity maps it to the opus bucket. Effort tiers are per model: an older Grok given `xhigh` fails at launch rather than downgrading. An Agy selector carries its own effort; check a selector with the zero-token `agy --model <selector> -p /model` and `-p /effort` (a fixed model reports `not adjustable`).

Opt-in only (`--add`; `adapters.OPT_IN`, pins checked by `doctor`): `agy:<third-party-selector>` (expensive, and correlated with the Sonnet lane), and `opencode:muse-spark-1.3-contributor-free` (Meta's Muse Spark 1.3 through OpenCode Zen's free tier). The Muse Contributor terms let Meta train on the prompts and on every file the lane reads, so add it only to a packet the operator is content to share: never client code, credentials, personal or unreleased material. Opencode lanes take Zen free models only (the `-free` suffix); paid Zen balance has no meter. Never: `agy:gpt-oss-120b-medium`; `agy models` lists it, but its agent loop aborts with no answer.

Gemini Pro and Flash are different evidence: keep Pro even when Flash also ends `done`. `done` certifies the output contract, not review quality. Same-family lanes (two Claude-family lanes, or `agy-<third-party-model>` under a Claude orchestrator) are correlated. Returns diminish after about three strong, independent peers; add a lane for a distinct capability or failure hypothesis, not for volume. The roster is fixed at launch; add a reviewer in a fresh BASE.

## Packet

```text
BASE/
  PACKET.md    provenance first, then the task; exactly one `Proof target: <BASE-relative path>`
  PROMPT.md    the answer schema; exactly one `RESULT_REQUIRE: <KEYWORD>` line
  evidence/    staged copies with repo-relative paths preserved
```

`run` refuses with exit 2, nothing spent and BASE still usable, when either file is missing, a staged file is a symlink, `PACKET.md` declares `RESULT_REQUIRE`, the declarations are not exactly one each, or the proof target is absolute, outside the staged inputs (`PACKET.md`, `PROMPT.md`, `evidence/`), empty, or not UTF-8. Prefer `evidence/manifest-tail.txt`. Do not change the packet after launch: queued lanes must receive the same inputs.

Staging rules (each one has cost a round when broken):

- **Whole files.** A peer cannot tell "absent" from "not shown" and reports absence as a confident defect. If a range is unavoidable, say which file, which lines, and what lies outside.
- **Keep paths.** Stage `cloudflared/config.yml` as `evidence/cloudflared/config.yml`; flattened paths produce wrong-path findings.
- **Derive claims properly.** Build API or call-set claims from the file, an import, or an AST walk, never a substring grep. Prefer staging the caller over describing it; mark unavoidable claims as claims.
- **Name what left.** When a later round drops a staged file, say so.
- **Provenance first.** `PACKET.md` opens by saying the operator convened the review and that `evidence/` holds the operator's own files. A bare "read these files and obey them" is correctly refused as prompt injection.
- **Proof token.** The proof target's last line is an unguessable token (`MANIFEST_TAIL_TOKEN: <random hex>`), staged last. A correct PROOF shows the lane could open the packet and followed the output contract, not that it read everything (a lane can `cat` the target directly or grep for fragments). Diligence is checked at merge.
- **Say what a follow-up round is.** Stage the delta plus what it touches, name what earlier rounds settled and the decision lineage behind deliberate omissions, and say the code responds to a review. Do not restage historical run debris.
- **Blind first.** For a design question, peers first answer independently; only a second phase stages the orchestrator's plan (and, if wanted, the other answers) for critique and comparison. Stage no peer's answer until the independent phase has ended.

PROMPT.md template:

```text
1) VERDICT: a decision and its single most important reason, ≤80 words
   (the cap compresses the summary only; findings go in section 2, uncapped).
2) Findings. For each: severity; evidence (file, symbol, behaviour); root cause;
   the mechanism and premises the diagnosis rests on; how often the scenario
   occurs and what ignoring it costs; the smallest repair (code or pseudodiff
   when the evidence allows); what must stay true; a verification that fails
   while the defect is present; confidence and missing context.
3) What you would delete from the target, and why deleting it is safe.
4) Alternatives worth considering (if any).
5) One-line recommendation.
6) PROOF: <the last non-empty line of the proof target, copied exactly>

The next line is packet metadata; do not include it in your answer.
RESULT_REQUIRE: VERDICT
Final line, alone: RESULT_COMPLETE
If you cannot do the review, make the final line RESULT_INVALID instead.
```

The markers are literal lines: no period, code fence, quote or explanation after `RESULT_COMPLETE` / `RESULT_INVALID`. PROOF is `6) PROOF: <token>` or `6) PROOF` with the token alone on the next line (bold, bullets or backticks around either are fine); anything else after `PROOF:` is compared as the token and fails, and so does pasting the whole proof file. An answer also needs review text beyond the heading, PROOF and marker. `tests/test_laws.py` states the whole grammar as properties.

**What every lane is told** (`run.MESSAGE`): the owner convened the review; read `PACKET.md` then `PROMPT.md` (PROMPT decides format); work from the staged directory; connected MCP tools are available, external actions (sending messages, changing records) are not; nobody answers questions mid-review. Codex, Claude and Agy use tools that read (plus web in live mode); Grok and opencode may also run Bash and write disposable scratch inside their own workspace, never the staged files. Stream lanes print the answer as their final message; app lanes write only `ANSWER.md`, in one complete write.

**What `run` adds to BASE**: `run.json` (roster, contract, settings, Herdr pane ids; written once), `lanes/<lane>/` (`message.txt`, `staged` stamp, `pids.json`, `exit.json`, `stream.jsonl` + `view.log` or `screen.log`, `stderr.log`, `harness.log` where the CLI supports one), `RESULT-<lane>.md` (the captured answer), `STATUS.json` / `STATUS.md` (one row per selected lane, skipped ones included). Peers work in stable workspaces `/tmp/agent-offload/.lanes/<lane>/`, restaged only after pool admission and reused by the next run of that lane; inspect a run's evidence in its BASE, not in a later run's workspace. Artifacts stay in `/tmp`; never copy run directories into the vault.

## Lane statuses

| Status | Meaning | Orchestrator action |
|---|---|---|
| `done` | answer ends `RESULT_COMPLETE`, has the required heading and review text, PROOF matches | read and adjudicate |
| `invalid` | an answer exists but breaks the contract (reason names how), or the peer declared `RESULT_INVALID` | read it anyway; its findings are leads; tell the operator |
| `failed` | no answer; reason is the harness's structured error, else its first stderr line | read `reason` + `stderr_tail`; tell the operator cause and remedy |
| `timeout` | idle limit, `--cap`, or the runner's own bound ended it | tell the operator; partial output is in the stream or `screen.log`, the last screen in `exit.json` |
| `skipped` | never spent: capacity reserve, auth, unknown capacity, or pool busy past `--cap` | tell the operator; `--force-lane` only on their say-so |
| `interrupted` | the runner or headless supervisor was signalled | reattach with `wait`, or rerun in a fresh BASE |

Never relaunch a lane that died of quota or auth in the same run. A lane lost to a bad packet or prompt is rerun in a fresh BASE after fixing the packet, and the operator is told. A lane that dies within seconds with a usage message usually means a flag drifted: read the first stderr line, run `xcheck doctor`, and compare the installed CLI's `--help` before blaming quota or the model. The only answer channels are a stream's final message and `ANSWER.md`; a review written under another name ends `failed` and is not scouted. Read its saved screen or stream before the lane's workspace is restaged.

## Placement and views

**Headless**: each lane runner is a child of the supervisor in its own session; its rendered output goes to `view.log`. A signal to the supervisor stops the lanes (`interrupted`).

**Herdr**: chosen when `HERDR_ENV=1`, `HERDR_PANE_ID` is set, `herdr pane layout` answers, and the tab is at least 176×14. The first lane splits right of the orchestrator (`--ratio 0.62`: the orchestrator keeps 62%); the rest split that column into equal rows in roster order, titled `xcheck:<lane>`. Panes are addressed only by the ids their split returned, never by title or focus, and never by `--current`. Each pane runs `exec <lane runner>`, so the pane is the runner: Herdr closes it when the runner exits, three seconds after its final line. Nothing closes panes by id. If the supervisor dies, panes finish on their own and `wait` reattaches.

**App view** (`tui`, default in Herdr; Agy always): the runner hosts the harness's real interactive app in a pty it owns, with the initial prompt as an argument, so nothing is typed into the app and there is no readiness to detect. App output reaches the pane unchanged; the operator's keystrokes reach the app. The answer is published once `ANSWER.md`'s last line is `RESULT_COMPLETE` or `RESULT_INVALID` and the file has not changed for 3 s; then the runner ends the app's whole process group. An app that draws nothing for its idle limit (a static dialog, a quota modal) times out with its last screen in `exit.json`; no prompt is resent.

**Stream view** (headless default): the harness's non-interactive mode; the runner reads its structured event stream, and the answer is its final message. Only stdout or a stable `ANSWER.md` resets the idle clock; stderr does not.

Completion is always the lane's `exit.json`, never pane text.

## Capacity

Each pool has one free meter that runs no inference, read by `xcheck/capacity.py`: Codex `app-server` `account/rateLimits/read` (5 h and weekly windows), Claude's OAuth usage endpoint with the keychain token (five-hour, seven-day, and the lane's opus/sonnet window when reported), Grok's billing credits view (the worse of the account and Grok Build meters; the monthly view reports no allowance on this account), and Agy's `agy -p /usage` (zero turns; the lane family's 5 h and weekly buckets). `xcheck capacity [--lanes …]` prints the readings. Each lane reads its own pool after admission and immediately before spending, so the reading is not stale and, in Herdr, runs outside any orchestrator sandbox. A lane spends only on `eligible`: the tightest window must have more than the reserve left, 10%, or `5 + 5n`% for the run's `n` lanes in one Agy family (capped at 90). The reserve protects the operator's interactive quota, which a runtime quota error arrives too late to protect. A meter that cannot be read is `capacity-unknown` and a rejected login is `auth-required`; with `reserve-low` they end the lane `skipped`. There is no fallback meter.

- Agy's `/usage` is served by its persistent headless `language_server`; the launchd agent `a launchd sidecar agent` keeps one running.
- Claude `security` exit 36 is a locked login keychain (a remote session): run `keychain-unlock-gui` with the operator at the screen, or pass a fresh reading from Claude Code's `/usage` as `XCHECK_CLAUDE_REMAINING=<percent left>` and `XCHECK_CLAUDE_REMAINING_AT=<ISO time>` (at most 5 min old) in the environment the runner inherits, i.e. headless placement. Exit 44 means no login.
- A rejected Grok token may only be stale: running `grok` once refreshes it.
- A reading is a snapshot. A healthy Agy family can still hit a per-model cap ("Individual quota reached"); that lane fails with the harness's own error and is not retried.

## Locking and liveness

A quota pool (codex, claude, grok, agy-gemini, agy-3p) is spent by one run at a time, for two reasons: a capacity reading means something only while no other run draws on that meter, and exact-path trust forces one stable workspace per lane name, which needs a single owner. Lanes of the same run share their pool; disjoint pools run in parallel. Under `/tmp/agent-offload/.pools/<pool>/`:

- `gate`: exclusive, held for an instant while one lane decides admission. `busy`: shared by the owning run's live lanes; only a gate holder takes it exclusively, and it converts to shared while still holding the gate. `owner`: the BASE that last took the pool; it says who may join an occupied pool and is not liveness evidence.
- A lane takes only its own pool, then reads capacity, stages its workspace and launches; it never waits for a second pool while holding one. Waiting for the gate and for `busy` share one deadline (`--cap`) and happen in the runner, without model calls.
- The harness inherits the `busy` descriptor, so a killed runner's orphan can spend only while the pool stays held, and the pool frees when the last holder exits. The app view ends its whole process group before the runner lets go.
- Never delete or replace a lock file: a new pathname is a different lock. All runs share the host-local root; `XCHECK_ROOT` exists for test isolation, not to bypass contention. The locks coordinate cooperating runs; they are not a security boundary.
- A descendant that leaves the process group and outlives both runner and harness is out of scope: nothing in scope spends quota that way. If it kept the inherited descriptor, later runs on that pool end `skipped: pool … stayed busy`; find the holder with `lsof +D /tmp/agent-offload/.pools`.

Runner and supervisor liveness is an exclusive lock on their own `alive` file, released by the kernel on crash: no PIDs, no process table (a sandboxed Codex orchestrator may be denied `ps`). `pids.json` and `supervisor.pid` exist for diagnosis. `BASE/.launched` (atomic mkdir, created after every refusal check) makes a BASE launch once; `wait` reattaches and never relaunches. Every runner writes `exit.json` exactly once and bounds itself with a kernel alarm at `2·cap + 300` s (pool wait and spend, plus capacity probe and shutdown); this holds even when no supervisor survives. A placed runner that never records itself within 90 s is settled `failed: lane runner never started`. A runner that dies without `exit.json` is settled by `wait`: it stops the orphaned harness only after `ps` confirms the command line names that lane's directory or workspace, never on a PID alone, and keeps any answer already published.

## Harness facts

`xcheck doctor` checks that every flag the adapters use appears in the installed CLI's `--help`, and the Grok/Agy model pins. Tests pin argv construction and captured stream shapes; neither proves a live tool call. After a CLI upgrade, re-verify a changed adapter with a small complete packet on a capacity-eligible lane. Self-update is off in every lane: Codex `check_for_update_on_startup=false`, `DISABLE_AUTOUPDATER=1` for Claude, `GROK_DISABLE_AUTOUPDATER=1`.

**Codex.** Stream: `codex exec` with `-s read-only`, the message on stdin, `--json`, and `-o` for the clean final message. Web is the `web_search` config key (`live|disabled`); `exec` has no `--search`, while the app's switch is `--search`. User config loads, so connected MCP servers are available; never `--ignore-user-config`. The read-only sandbox still lets it run read commands anywhere. Errors arrive as a top-level `error` event and `turn.failed.error.message`. The app runs `-s workspace-write -a never` so it can write `ANSWER.md`, with `history.persistence="none"`. It shows a trust screen for any directory not trusted by exact path (a trusted parent does not count; `-c projects…trust_level` on the command line does not suppress it), so `xcheck/trust.py` records each stable workspace under both `/tmp` and `/private/tmp` spellings in `~/.codex/config.toml`. It edits only the canonical `[projects."<dir>"]` + `trust_level = "…"` layout and refuses anything else; a TOML round-trip must show only those trust levels changed. On refusal, trust the workspace by hand; never auto-click a dialog or normalise the file.

**Claude Code.** `--restricted` ignores settings files and hooks, confines the file tools to the working directories (a read of `/etc/hosts` is refused), and, despite its help text, does not load the operator's MCP servers from `~/.claude.json`; account connectors (claude.ai Gmail, Drive, …) load either way. So `--mcp-config ~/.claude.json` loads them, once. `--tools` lists what exists (Read, plus WebSearch/WebFetch in live mode, plus Write in the app); `--allowedTools` pre-approves that list plus `mcp__<server>` for every server in `~/.claude.json` and every `claudeAiMcpEverConnected` connector (`claude.ai Gmail` → `mcp__claude_ai_Gmail`). `mcp__*` is not honoured as a pattern, and `bypassPermissions` is refused in restricted mode, so each server is named. A server added after launch is denied in that run. Keep both lists: without `--allowedTools` a tool would prompt. Stream mode adds `--permission-prompts none` (anything that would prompt is denied instead of hanging) and `--include-partial-messages` (keeps the stream moving through a long think, so the idle clock measures real silence). Never `--bare`: it skips the keychain, and subscription auth fails. The final text is the `result` event; with `is_error: true` that field is the error message even when `subtype` says `success`, so it is never an answer. `rate_limit_event` carries five-hour and seven-day utilisation.

**Grok.** `--sandbox read-only` reads everywhere and kernel-denies writes outside `~/.grok` and tmp. `strict` cannot read `/opt/homebrew`, so the operator's global hooks that exec a binary fail with exit 126 and Grok ignores the failure; `off` removes write protection. The operator's shell exports `GROK_SANDBOX`, which would otherwise choose the profile, so the lane environment drops it. Re-verify with `--debug-file` and grep `hook failed` / `exit code 126`. `--always-approve` lets Bash and connected MCP tools run without a prompt in both views. `--tools` is honoured only headless; the app ignores it. Memory is off through `GROK_MEMORY=0` (`--no-memory` is accepted but absent from `--help`). The stream answer is the last model message after the last `tool_call`; a `usage` event closes each message, so earlier narration is never glued onto the answer. Errors are `{"type":"error","message":…}`. Docs: `~/.grok/docs/user-guide/` (10-hooks, 14-headless-mode, 18-sandbox, 20-background-tasks).

**Agy** always runs as its app, headless placement included: print mode soft-denies a tool confirmation after a sandbox-blocked command and ends the whole turn with no answer. The app runs `--sandbox --dangerously-skip-permissions --mode accept-edits`, so nothing asks. Never pass `--effort` (the selector carries it; fixed selectors reject the flag), and never `--disable-slash-commands` (it also disables plan mode). Its file tools reached every calibration fragment of a 952 KB, seven-file packet on both Flash and Pro, so no inline bundle or size cap applies; that proves reach, not exhaustive reading. Agy starts its own configured MCP servers, exposes a generic `call_mcp_tool`, and has no tool allowlist, so its external-action and packet-mode web limits are instruction-only. `xcheck/trust.py` adds each workspace to `trustedWorkspaces` and sets `showFeedbackSurvey=false` in `~/.gemini/antigravity-cli/settings.json`.

**opencode** (1.18.32). Zen's free tier serves only opencode's stock tool set: any permission rule, even a lone `bash: deny` in `OPENCODE_CONFIG_CONTENT` or a custom agent, is refused with 403 `FreeTierError: free tier can only be used from within OpenCode`. So the tools stay stock and the lane environment drops every `OPENCODE_*` variable. `--auto` answers permission asks, because an unanswered ask in `opencode run` waits silently forever. The write boundary is the OS: a Seatbelt profile (`sandbox-exec -p`, `adapters.opencode_sandbox`) allows writes only in the lane workspace, opencode's own state and cache, and the per-user temp tree `/private/var/folders`. Bash and the edit tools get `Operation not permitted` anywhere else, and MCP servers that must write elsewhere (uv-launched ones such as semble) fail to start. Startup can take a minute while MCP servers load; that is silence inside the stream idle limit. Stream: `opencode run --format json`; the answer is the text after the last `tool_use`, and errors arrive as `{"type":"error","error":{"data":{"message":…}}}` (a free-usage limit is `FreeUsageLimitError`, HTTP 429, with no published quota). App: `opencode <work> -m … --prompt …`. Web and packet-mode limits are instruction-only here, as for MCP.

**Herdr** (0.9.1). `pane layout --pane <id>` gives tab size; `pane split --pane <id> --direction right|down --ratio <share kept> --cwd <dir> --no-focus` returns `.result.pane.pane_id`; `pane run <id> <command>` types into the pane's shell (typeahead is buffered, so no readiness wait is needed); a pane closes when its process exits.

**Lane environment.** The orchestrator exports its session identity into every child shell (`CLAUDECODE`, `CLAUDE_CODE_*`, `CLAUDE_PID`, `CLAUDE_EFFORT`, `CLAUDE_PLUGIN_*`, `CODEX_COMPANION_*`, `CODEX_SANDBOX*`, `OPENCODE*`, `HERDR_*`). A peer that inherits them becomes a nested session of the orchestrator, runs at its effort, or can drive the operator's Herdr. The runner removes them, `GROK_SANDBOX`, and every `*_API_KEY` (lanes use subscription auth; a key would bill instead). Harmless preferences such as `GROK_TELEMETRY_*` pass through. Re-verify: `env` in the orchestrator's shell, then the fake-harness env test.

**Operator rules reach peers.** Codex lanes load `~/.codex/AGENTS.md`; Grok loads `~/.agents/rules` through `extra_rule_dirs` (a `GROK_CONFIG` overlay does not clear it). Relocating `CODEX_HOME` / `GROK_HOME` / Agy's home to isolate them would split the auth files and risk rotated credentials, so the lane message states the review task directly and scopes those rules to it. Re-verify by asking a low-effort lane to quote the global instructions it received.

**Independence.** Local commands and MCP tools can read outside the workspace, including another lane's result. Separate copies and the instruction prevent deliberate sharing, not adversarial reading.

## Invariants

| # | Invariant | Enforced by |
|---|---|---|
| I1 | Peers are harnesses other than the orchestrator's; all receive the same immutable packet | `--orch` exclusion, per-lane copies, launch-once |
| I2 | Evidence is whole files with preserved paths and a proof token staged last | staging rules, preflight, PROOF check |
| I3 | No manual approval, trust click or typed prompt is ever part of a run | permission flags, exact-path trust, prompt as argv |
| I4 | Every lane ends with one `exit.json`, bounded by idle, cap, and the runner's kernel alarm | lane runner |
| I5 | No silent shrinkage: every selected lane has a status row, and every non-`done` row is told to the operator | `STATUS.md` + orchestrator duty |
| I6 | Capacity is read under pool admission right before spend; nothing retries automatically | lane runner, `xcheck/capacity.py` |
| I7 | Only a stream's final message or a stable `ANSWER.md` is an answer | lane runner, `validate.py` |
| I8 | One launch per BASE; reattachment never relaunches | `.launched`, `wait` |
| I9 | Kernel locks, never PIDs or the process table, prove a live holder | `alive` and pool `flock`s |
| I10 | One runner and one validation path serve both placements and both views | `xcheck lane` |
| I11 | A lane name states the exact harness, model and effort launched | `adapters.Lane.name` |
| I12 | The orchestrating model spends no turns while lanes work | one blocking call, or `--detach` + `wait --timeout` |
| I13 | Lanes receive neither the orchestrator's identity nor any `*_API_KEY` | lane environment scrub |

## Current limits

- Connected MCP tools run without per-call review. The ban on external actions and packet-mode web limits is an instruction, not a tool boundary; `--web packet` is not a security boundary for untrusted material.
- Grok can write anywhere in tmp, and opencode anywhere in `/private/var/folders`, including another lane's scratch; their own-workspace limit is an instruction.
- A static account-verification splash can hit the 180 s app idle; the last screen is kept and the lane ends `timeout`.
- A Herdr failure after a split but before `pane run` leaves a pane with an idle shell; close it by hand. (A failed `exec` makes zsh exit, so that pane closes itself.) Either way the lane is settled `failed: lane runner never started` after 90 s.
- `--cap` ends a lane; there is no late harvest. Rerun in a fresh BASE.
- If a runner died and `ps` is unavailable, `wait` cannot confirm the orphan's identity and leaves it alone, noting that in the reason; its pool stays held until the orphan exits. Never kill a recorded PID on a guess.
- Real quota and auth error streams have not been captured; invalid-model fixtures prove parser shape only. Keep the next real one as a fixture.

## Deliberately absent

Legacy (archived read-only in `../x-check-old/`, never a launcher) solved problems this design does not have. Do not re-add these without a failure that happened within scope:

| Legacy mechanism | Why it is not needed |
|---|---|
| Credit/auth log-regex classifiers | their only consumer was the retry decision; structured errors name the cause and nothing retries |
| Late harvest, file scouting, provisional labels, detached cleanup worker, notices file | lanes are owned processes that end with `exit.json`; one declared answer channel; the orchestrator reports from `STATUS.json` |
| Spender claims, PGID fallback, boot identity, daemon reaper, global lock with live-lane scans | every spender is our runner or its child; kernel lock lifetime equals spender lifetime; per-pool admission keeps disjoint pools parallel |
| Supervisor watchdog, pane-close and pane-ownership bookkeeping | the runner's alarm bounds it; `exec` makes each pane close with its runner |
| Herdr `agent start` readiness, splash classifiers, prompt resend | the prompt is argv in a pty; a static screen becomes a bounded timeout |
| Agy stdin bundle, 192 KB cap, read-depth probe | file tools reach large packets; the probe was gameable by grep |
| Host router with note read-map, wait metrics, `LIVE_CHECK_CAP` gate | `--mode auto` places; the note fits one read; token discipline is structural; the cap was a prompt contract, not enforcement |
| Perplexity lane | not a default peer; its failure shapes stay documented in Legacy |
| An opencode reviewer agent or permission rules | the free tier refuses anything but opencode's stock tool set; the Seatbelt profile is the boundary |
| Runtime under `~/.agents` with per-start hash verification | `~/.local` is outside Codex's writable roots, so the read-only snapshot needs no re-verification |
| Snapshot pruning | a queued runner may still exec an older snapshot |
| Fallback quota meters (Agy language-server port scan, CSRF probing, desktop-token scraping; Grok monthly view), an outer retry layer, secret redaction of notes | each pool's one meter works; an unreadable meter fails closed; notes never carry payloads or tokens |

Proposals already rejected, with the reason:

| Proposal | Why not |
|---|---|
| Isolated HOME for Agy (or Codex/Grok) | splits auth files |
| Remove Claude `--allowedTools` | `--tools` only lists tools; `--allowedTools` is what pre-approves them |
| Kill an orphan when `ps` is denied | PID reuse can kill an unrelated process |
| Shell-readiness wait before `pane run` | typeahead is buffered; a miss surfaces as `failed` |
| Scrub loader variables (`DYLD_*`, `PYTHON*`) | the installed copy runs `python -I`, and the Codex rule matches only the exact command |
| `try/finally` around pool release | process exit releases the lock |
| A bare `xcheck` pattern in the Codex rule; allowing `lane`/`install` | PATH hijack; those are not orchestrator commands |
| Automatic retries | a quota/auth death repeats; a bad packet needs a fresh BASE |

## Merge

1. Re-read `STATUS.json` and every `RESULT-*.md`, `invalid` ones included, right before merging. A truncated draft is a lead only; never manufacture a peer's final position from it.
2. Check each concrete claim's **premises** against the real tree before its reasoning. The ratifying lane fails at the premise: it can reject a correct fix because it misread code it was shown. Absence-of-code, wrong-path and packet-claim findings are packet suspects: open the real file first.
3. Never tally reviewers. Agreement counts only when premises were derived independently; same-family lanes are correlated. Volume is not quality, and a small delta is no reason to drop the expensive lane.
4. Judge each repair separately from its diagnosis: a correct diagnosis does not make its repair correct. The verification must be able to fail while the defect is present; two checks that read the same state are one signal.
5. Write `CONSENSUS.md`: one decision line; a confirmed / rejected / uncertain table with lane provenance; for each confirmed finding the selected repair, its verification, and why the host chose it when it differs from the peer's; lanes with no usable answer, never counted as disagreement. If no peer produced substantive evidence, say the review is unverified and why; relaunching an unchanged packet is not progress.
6. Review applied fixes in a fresh round scoped to the delta and the contracts it touches, with the decision lineage staged. Stop when no admitted finding remains.

**Admission rule.** A finding earns code only if its failure has happened or is likely within scope, and then it gets a tested repair. A real but out-of-scope finding earns one line in Current limits. Anything else is rejected in `CONSENSUS.md` with its reason. Ask reviewers what they would delete, not only whether something is correct. Never add prose to pre-empt a reviewer. A change keeps or reduces total size unless it adds an operator-requested capability.

Fixes go into the reviewed project with a test; protocol lessons go into this note as rules, not stories.

## Repository

`python3 -m unittest discover -s tests` (about two minutes; run it in the background) and `git diff --check`. Runtime is Python 3.11+ stdlib plus the harness CLIs. Hypothesis (`requirements-test.txt`) is the one test dependency, imported unguarded so a missing install fails by name. `tests/test_xcheck.py` runs end to end against `tests/fake/harness`, which speaks each CLI's stream and app shape, plus captured streams and meter payloads in `tests/fixtures/`; `tests/test_laws.py` holds property laws for the answer grammar, the Grok reader and trust updates. Tests set `XCHECK_ROOT`, `CODEX_CONFIG` and `AGY_SETTINGS` to disposable paths, and the fake `codex`/`agy` also answer the capacity meters (`FAKE_CAPACITY`); never point them at the real `/tmp/agent-offload` (they would queue behind live runs) or the real configs. Dropbox resets new files to mode 600; restore `755` on `bin/xcheck` and `tests/fake/harness`.

After changing `bin/` or `xcheck/`, run `bin/xcheck install`, then `xcheck doctor`. History is `git log`; durable requirements, invariants and limits live here, not in TODO or ledger files.
