---
aliases: [Multi-Agent Cross-Check, Multi-Agent Cross Check Protocol, multi agent cross check, X-Check]
tags: [multi-agent, cross-check, claude-code, codex, grok, agy, herdr]
---

# Multi-Agent Cross-Check Protocol

Fan a staged review packet out to **other** agent CLIs (Codex, Claude Code,
Grok, Agy) as real processes, collect each one's answer as a file, and merge
them yourself. Two placements of the same lanes: **headless** (no windows) and
**Herdr** (one visible pane per lane, right of the orchestrator).

This is **X-Check** and its own git repo. `bin/xcheck` is the only entry
point. **X-Check Legacy** is archived read-only in `../x-check-old/`; consult
it for a safeguard's original failure mode, never as an alternate launcher.

Division of labour:

| Owner | Holds |
|---|---|
| This note | invariants, packet contract, harness facts, merge rules |
| `bin/xcheck` + `xcheck/` | everything with one mechanical right answer: exact flags, process groups, deadlines, stream parsing, result validation, status, pane placement |
| The orchestrating agent | judgment: roster, packet, reading a failed lane's error and telling the operator, adjudication, the merge |

The orchestrator never improvises launch mechanics and the code never makes a
judgment call (no log-regex classifiers, no automatic retries).

---

## Invariants

| # | Invariant | Enforced by |
|---|---|---|
| I1 | Peers are harnesses other than the orchestrator's; all get the same packet; none is shown another's answer | `run --orch` refuses a same-kind lane; per-lane workspace copies |
| I2 | Complete evidence: whole files, repo-relative paths, a proof token as the last staged line | packet rules below + `Proof target:` + PROOF check |
| I3 | Peers may use connected MCP tools; web search is on unless `--web packet`; no approval click is part of a run | explicit permission modes, tool approvals and exact-path workspace trust. Local restrictions differ by harness; MCP external-action and web limits are instructions, not enforcement |
| I4 | Live runners enforce inactivity and spending budgets on their process groups; the supervisor has a separate stuck-runner watchdog | lane runner + supervisor; loss of all supervision is not a host-wide containment guarantee |
| I5 | No silent shrinkage: every selected lane has a status row, and every non-`done` row is told to the operator in chat | `STATUS.md` + orchestrator duty |
| I6 | Capacity is checked before spending; a refusal requires an explicit override; peers are never retried automatically | capacity preflight; `--force-lane` names a selected lane; `--skip-capacity` is only for controlled smokes |
| I7 | Files are the truth: stream view validates the harness's final message; TUI view validates a stable `ANSWER.md` | lane runner + `xcheck/validate.py` |
| I8 | One launch per BASE | `BASE/.launched/` (atomic mkdir) |
| I9 | Losing the orchestrator does not require relaunching already-started work; independent runners publish their own status | `xcheck wait --base` reattaches without another fan-out |
| I10 | Cooperating runs coordinate each quota pool separately; same-run lanes share it and disjoint pools run in parallel | per-pool `flock`, held by the runner and inherited by its harness, under the lifetime assumptions below |
| I11 | Lane name is provenance: `<harness>-<exact model>[-<effort>]`, and that model is what is launched | `adapters.Lane.name` |
| I12 | The orchestrating model spends no turns while lanes work | one blocking call that prints only the final table; `--detach` + `wait --timeout S` where a tool call cannot hold that long |
| I13 | The launcher does not forward the orchestrator's session identity or `*_API_KEY` environment values | lane environment scrub; subscription auth remains in each harness's own account store |
| I14 | Liveness is proven by the kernel, never by PIDs or the process table | each runner and supervisor holds an exclusive lock on its own `alive` file; alive = that lock is taken |

Scope: one operator, one machine (macOS), attended runs, several orchestrator
sessions possibly running cross-checks at once. A reviewer finding that needs
a reboot mid-run or an unattended multi-hour wait is out of scope unless a real
run hit it.

### Why these mechanisms stay

| Mechanism | The requirement it serves |
|---|---|
| One runner, two placements | Headless and Herdr must share deadlines, validation and settlement; Herdr supplies windows, not a second execution engine |
| Per-pool locks, not one global lock | Runs sharing a subscription must serialize; independent subscriptions must not block each other. Stable lane workspaces also require exclusive ownership across runs |
| Inherited lock descriptors and group cleanup | A runner's death must not free quota while its harness still spends; a finished TUI must not leave children holding the pool |
| File-based answers and one launch per BASE | A changing screen is not a review, and reattachment must not create a second paid attempt |
| Capacity preflight | A runtime quota error comes too late to preserve the operator's reserve |
| Judgment outside the runner | Finding validity, failure diagnosis and merge quality cannot be reduced to marker checks or error-keyword voting |

The runner does not need modal classifiers, prompt-resend recovery, a second
launcher, or a background late-harvest service. It starts each prompt once,
owns the process group, captures the declared answer channel, and settles
within its budgets. Non-contract scratch files are disposable. These are
explicit boundaries, not claims that the omitted legacy behavior is equivalent.

The underlying primitives are documented in Python's
[subprocess interface](https://docs.python.org/3/library/subprocess.html)
(`pass_fds`, `start_new_session`) and the BSD
[flock contract](https://man.openbsd.org/flock.2). Locks coordinate cooperating
runners; they are not a filesystem security boundary.

### Locking contract

The rewrite initially chose one global lock under a single-run scope
(`568739a`). Per-pool admission (`11806a2`) broadened that scope without
blocking unrelated subscriptions; inherited descriptors (`22b8db1`) extended
ownership beyond the runner. These are design choices with different
assumptions, not a requirement to recreate Legacy's claim manager.

- Each lane acquires only its own pool, then reads capacity, stages its
  workspace and launches. It never waits for a second pool while holding one.
- The short exclusive `gate` serializes admission. The shared `busy` lock
  permits lanes bearing the same BASE owner to coexist. The exclusive-to-shared
  transition happens while the gate is held.
- The owner tag identifies who may join an occupied pool; it is not liveness
  evidence. An unlocked pool may change owners despite stale metadata.
- Waiting for either gate or pool consumes the same bounded admission budget.
  Waiting happens in the runner, without model calls or orchestrator polling.
- A pool releases independently when its last inherited holder exits or closes
  its descriptor, not when the whole batch finishes. Never delete live lock
  files: recreating a pathname would create a different lockable inode.
- All participating runs use the same host-local root. `XCHECK_ROOT` is a test
  isolation override, not a way to bypass contention. Do not run Legacy and
  X-Check concurrently against the same accounts: their lock namespaces differ.

This protects ordinary supervisor loss and a runner crash while its harness
retains the descriptor. It does not cover a descendant that drops that
descriptor and outlives **both** holders; a synthetic fault test demonstrates
that limit. There is no automatic PGID-claim fallback, remote quota reservation,
or general daemon reaper. Any extension must justify its failure model and
operating cost against this rewrite's requirements, not Legacy's mechanisms.

---

## Run it

```bash
xcheck doctor                                          # installed copy, flags, model pins
xcheck run --base /tmp/agent-offload/<slug> --orch claude   # default roster minus claude
```

`xcheck` on PATH is `~/.local/bin/xcheck`, a read-only copy published by
`bin/xcheck install` from this folder. Install records the source path;
`doctor` compares its current digest even when invoked through the installed
copy. Reinstall after changing `bin/` or `xcheck/`. Codex's allow rule matches
only the **absolute** `~/.local/bin/xcheck` path followed by `run`, `wait`,
`status`, or `doctor`. Use that absolute path in Codex tool calls, with no
`NAME=value` prefix, `cd … &&`, pipe, or redirect. `lane` and `install` are
not in the rule. Installed snapshots are not pruned automatically: a queued
runner may still be using an older one.

1. **Stage** `BASE=/tmp/agent-offload/<slug>/` (packet contract below).
2. **Record** BASE, roster, and the exact command in the existing task tracker
   or chat before spending. Do not create another handoff document just for a
   run; the launcher persists its settings in `run.json`.
3. **Announce** the roster in chat: `Fan-out: N peers → <lanes>. Not peers: <harness> (<why>)`.
4. **Run** it without spending turns (I12). The call blocks until every lane has
   ended and prints only the status table; do not read panes, files, or logs
   while it runs. Pick the form your harness can hold:

   | Orchestrator | Form |
   |---|---|
   | Claude Code | `xcheck run …` as a background shell job (`run_in_background`); the completion notice is the one return |
   | opencode | `run … --detach`, then `wait --base B --timeout S` with the shell call's `timeout` set above S. Its shell tool (1.18.32) defaults to 120 s, runs each command in its own process group, and has no background parameter; the detached supervisor is in its own session, so it survives the call |
   | Grok | Herdr mode only (its sandbox covers its whole process tree; `run` refuses headless). Run `xcheck run` as a background command, then one `get_command_or_subagent_output` with `timeout_ms` up to 3 600 000 (user-guide 20-background-tasks) |
   | pi | `run … --detach` + `wait --timeout <call cap>`; never pi's own `delegate`/`subagent` tools as peers (they start pi) |
   | Codex | `~/.local/bin/xcheck run … --detach`, then `~/.local/bin/xcheck wait --base B --timeout S` under the call's timeout. `~/.codex/rules/xcheck.rules` (written by `install`) runs these public commands outside the sandbox with no approval; its target is the read-only copy under `~/.local`, which the sandbox cannot write |
   | Agy | allowed; every Agy lane is dropped (`--orch agy`) |
   | Devin and other harnesses not in the peer roster | use `--orch other`; one blocking shell wait, or `--detach` followed by `wait --timeout S` within the tool's call limit |

   `wait --timeout S` exits 3 while lanes still run, so each extra return costs
   one short message. Stage and launch before loading unrelated material into
   the orchestrator: every return re-sends its whole context.
5. **Report** every non-`done` lane in the next chat message, using its
   `reason` and `stderr_tail` from `STATUS.json`:
   `<lane> <status> — <cause in plain words>. Fix: <remedy>. Continuing with <others>.`
6. **Merge** (rules below) into `CONSENSUS.md` in BASE.

`run` options: `--lanes kind:model[:effort],…` replaces the roster;
`--mode auto|headless|herdr` (auto = Herdr when available); `--web live|packet`
(default `live`: peers may search the web and cite what they used; `packet`
disables built-in web tools and instructs peers not to fetch, but connected MCP
tools remain available, so it is not an isolation boundary for untrusted input);
`--idle` (900 s without stream output, 180 s without TUI output by default,
including Agy's app in an otherwise stream-view run) / `--cap` (3600 s spend budget per
lane); `--force-lane NAME` launches past a capacity refusal (say so in chat);
`--skip-capacity` for smoke runs; `--detach`. A lane whose pool is held by
another run waits in its own process (no orchestrator turns) for up to `--cap`,
then ends `skipped`.

Select at least one peer. Explicit `--idle` and `--cap` values must be positive;
every `--force-lane` name must belong to the selected roster. Invalid launch
options are refused before creating panes, launching models, or consuming BASE.
Use `--orch other` for an orchestrator not listed by the CLI; do not pretend to
be a different supported peer harness to bypass same-harness exclusion.

Other commands: `xcheck wait --base B [--timeout S]` (block until done; also
reattaches and finishes the job if the supervisor died), `xcheck status --base B`
(render from files), `xcheck doctor`, `bin/xcheck install`.

Exit codes: 0 finished (read the table), 2 refused before any spend
(packet, launch-once, orchestrator rule), 3 still running (`wait
--timeout`), 130 supervisor interrupted.

---

## Default roster

| Lane | Pool |
|---|---|
| `codex-gpt-6-sol-high` | codex |
| `claude-opus-5-5-high` | claude |
| `grok-4.7-xhigh` | grok |
| `agy-gemini-3.1-pro-high` | agy-gemini |
| `agy-gemini-3.8-flash-high` | agy-gemini |
| `agy-<third-party-model>` | agy-3p |

The orchestrator's own harness is removed (`--orch`). `--orch agy` removes all
Agy lanes. Pins live in `xcheck/adapters.py`; `xcheck doctor` confirms Grok and
Agy pins appear in `grok models` / `agy models`. Refresh pins deliberately; the
lane name changes with them. Claude pins use the versioned id
(`claude-opus-5-5`, not `opus-5-5`); capacity maps it to the opus bucket.

Opt-in only: `agy:<third-party-selector>` (expensive; correlated with the
Sonnet lane when both run). Never: `agy:gpt-oss-120b-medium`: `agy models` still
lists it, but its agent loop aborts with `Agent execution terminated due to
error.` and no answer.

The two Gemini lanes are not interchangeable. Keep Pro in the default roster;
do not replace it with Flash merely because both produce `done` answers.
`done` certifies the output contract, not the quality of the review. Same-family
responses are correlated evidence, not independent votes.

---

## Packet contract

```text
BASE/
  PACKET.md        provenance first, then the task; exactly one `Proof target: <BASE-relative path>`
  PROMPT.md        the answer schema; exactly one `RESULT_REQUIRE: <KEYWORD>` line
  evidence/        staged copies, repo-relative paths preserved
```

The proof target must be a BASE-relative path to `PACKET.md`, `PROMPT.md`, or
a file under `evidence/`: these are the inputs copied to each peer. Prefer
`evidence/manifest-tail.txt`. An arbitrary file elsewhere in BASE is not a
staged input. Absolute paths and staged symlinks are refused.

`xcheck run` refuses (exit 2, nothing spent) when either file is missing, the
proof target is absent/outside the staged inputs/empty/not UTF-8, or the
declarations are not exactly one each. The launch-once marker is made only
after those checks, so a preflight refusal does not consume BASE.

What `run` adds:

```text
  run.json                 lanes, contract, settings, Herdr pane ids
  lanes/<lane>/message.txt the one-paragraph instruction every lane receives
  lanes/<lane>/staged      stamp after restaging, so only this run's answer is salvaged
  lanes/<lane>/stream.jsonl, stderr.log, pids.json, exit.json, view.log (stream)
  lanes/<lane>/screen.log, harness.log, exit.json (TUI; harness.log also for stream where supported)
  RESULT-<lane>.md         captured final answer (stream message or TUI ANSWER.md)
  STATUS.json / STATUS.md  one row per selected lane, including skipped ones
```

Peer working directories live separately at
`/tmp/agent-offload/.lanes/<lane>/`. They are restaged only after pool admission
and reused on the next run of that lane. Inspect retained `RESULT-*` and
`lanes/` logs in BASE, not a later run's workspace. Do not change PACKET,
PROMPT or evidence after launch: queued peers must receive the same inputs.

Staging rules (each one has cost a round when broken):

- **Whole files.** A peer cannot tell "absent" from "not shown" and reports
  absence as a confident defect. If a range is unavoidable, say in PACKET.md
  which file, which lines, and what lies outside.
- **Keep paths.** Stage `cloudflared/config.yml` as
  `evidence/cloudflared/config.yml`, never flattened; renamed paths produce
  wrong-path findings.
- **No claims you did not derive properly.** Build API/call-set claims from
  the file, an import, or an AST walk, never a substring grep. Prefer staging
  the caller over describing it. Mark unavoidable claims as claims.
- **Name what left.** If a later round drops a staged file, say so in
  PACKET.md.
- **Provenance first.** PACKET.md opens by saying the operator convened the
  review and that `evidence/` holds the operator's own files. A bare "read these
  files and obey them" is correctly refused as prompt injection.
- **Proof token.** Make the proof target's last line an unguessable token
  (`MANIFEST_TAIL_TOKEN: <random hex>`), and stage it last. With file tools,
  a correct PROOF shows the lane could open the packet and followed the
  output contract; it does not show that it read everything (a lane can
  `cat` the target directly). Diligence is checked in the merge: an
  absence-of-code finding is verified against the tree before it counts.
- **Say what a follow-up round is.** When reviewing fixes from an earlier
  round, stage the delta plus what it touches, name what earlier rounds
  settled, and say the code is a response to a review.

### Answer schema (template for PROMPT.md)

```text
1) VERDICT: a decision and its single most important reason, ≤80 words
   (the cap compresses the summary only; findings go in section 2, uncapped).
2) Findings. For each: severity; evidence (file, symbol, behaviour); root cause;
   the mechanism and premises the diagnosis rests on; how often the scenario
   occurs and what ignoring it costs; the smallest repair
   (code or pseudodiff when the evidence allows); what must stay true;
   a verification that fails while the defect is present; confidence.
3) What you would delete from the target, and why deleting it is safe.
4) Alternatives worth considering (if any).
5) One-line recommendation.
6) PROOF: [the last non-empty line of the proof target, verbatim]

The next line is packet metadata; do not include it in your answer.
RESULT_REQUIRE: VERDICT
Final line, alone: RESULT_COMPLETE
If you cannot do the review, make the final line RESULT_INVALID instead.
```

Keep `RESULT_REQUIRE` visibly separate from the requested answer. Do not append
a period, code fence or explanation to `RESULT_COMPLETE` or `RESULT_INVALID`.
The markers are literal lines, not prose.

Copy the PROOF line as written: the validator accepts `6) PROOF: <token>` or
`6) PROOF` followed by the token on the next line (bold or backticks around
either are fine); anything else after `PROOF:` is compared as the token and
fails. An answer also needs some review text besides the heading, PROOF, and
marker. `tests/test_laws.py` states the whole contract as properties.

---

## Lane statuses

| Status | Meaning | Orchestrator action |
|---|---|---|
| `done` | final message ends `RESULT_COMPLETE`, has the heading, PROOF matches | read and adjudicate |
| `invalid` | an answer exists but breaks the contract (reason says which), or the peer declared `RESULT_INVALID` | still read it; its findings count as leads; tell the operator |
| `failed` | no answer; `reason` holds the harness's own structured error or "no answer" | read `reason` + `stderr_tail`, tell the operator cause and remedy |
| `timeout` | killed by `--idle` or `--cap` | tell the operator; partial output is in `stream.jsonl` |
| `skipped` | not launched: capacity reserve, auth, or unknown capacity | tell the operator; `--force-lane` only on their say-so |
| `interrupted` | the supervisor was signalled (headless) | reattach or rerun in a fresh BASE |

Never relaunch a lane that died of quota or auth in the same run. A lane lost
to a bad packet or prompt is rerun in a fresh BASE after fixing the packet,
and the operator is told.

---

## Modes

**Headless.** Each lane runner is a child process of `xcheck run`, in its own
session; its rendered view goes to `lanes/<lane>/view.log`.

**Herdr.** Chosen when `HERDR_ENV=1`, `HERDR_PANE_ID` is set, `herdr pane
layout` answers, and the tab is at least 176×14. The first lane splits right of
the orchestrator (`--ratio 0.62`, orchestrator keeps 62 %); the others split
down that column into equal rows, top-to-bottom in roster order. Each pane is
titled `xcheck:<lane>` and runs the lane runner with `pane run`.

**Views.** `--view tui` (default in Herdr): each pane shows the harness's own
interactive app, full screen. The runner starts it in a pseudo-terminal it
owns, with the initial prompt as a command-line argument (`claude "…"`,
`codex "…"`, `grok "…"`, `agy -i "…"`), so nothing is typed into the app and
there is no readiness to detect. The app's output reaches the pane unchanged
and the operator's keystrokes reach the app. The lane answers by writing
`ANSWER.md` in its workspace; the runner publishes it once its last line is
`RESULT_COMPLETE` (or `RESULT_INVALID`) and it has not changed for 3 s, then
ends the app. A lane that stops drawing for its view's `--idle` (a static dialog, a quota
modal) times out with its last screen in `exit.json`. `--view stream`
(default headless): the harness's non-interactive mode, rendered from its
structured event stream; the answer is its final message. Completion is
always the lane's `exit.json`, never pane text. After a lane ends its pane
lingers 3 s, then the supervisor closes it, only if it is still in the
caller's tab and is not the caller.

TUI-mode flags per harness live in `xcheck/adapters.py` (`tui`): Claude adds
`Write` to its tools and preapproves connected MCP tools; Codex runs
`-s workspace-write -a never` and `--search` in live mode; Grok keeps Bash
and connected MCP tools available with `--always-approve` under the `read-only` OS
sandbox (writes allowed under
`~/.grok` and tmp, including other lanes' tmp files); Agy runs `--sandbox
--dangerously-skip-permissions --mode accept-edits -i`. Before Codex or Agy
starts, `xcheck/trust.py` idempotently records the exact stable workspace in
their user configs, under both `/tmp` and `/private/tmp` spellings. It also
sets Agy's `showFeedbackSurvey=false`; an approval/trust click is never part
of a run.

If the supervisor dies in Herdr mode the panes keep running and finish on
their own. `xcheck wait --base B` reattaches, waits, renders status and closes
the panes. In headless mode a supervisor signal stops the lanes (`interrupted`).

---

## Capacity

`bin/agent-capacity` (carried from v1) reads each pool's free subscription
meter without spending a prompt: Codex app-server rate limits, Claude OAuth
usage, Grok billing (weekly credits view), Agy's local quota summary (per
family). Each lane runner probes its own pool immediately before spending,
so in Herdr mode the probe runs in the pane, outside any orchestrator sandbox.
A lane launches only on `eligible`; the reserve is 10 %, and for Agy families
it is `5 + 5n` % for the `n` lanes of this run in that family (capped at 90).
`reserve-low`, `auth-required`, and `capacity-unknown` end the lane as
`skipped`, which must be announced.

Agy readings come from an Antigravity `language_server`; the launchd agent
`a launchd sidecar agent` keeps one running when the desktop app is closed.
Claude's `security` exit 36 in the probe means a locked login keychain: run
`keychain-unlock-gui` (operator at the screen), or pass a manual reading via
`AGENT_CAPACITY_CLAUDE_MANUAL_REMAINING` + `…_CAPTURED_AT` (≤5 min old).

Capacity is a snapshot, and a healthy Agy family reading does not rule out a
per-model cap ("Individual quota reached"). A lane that hits a cap mid-run
fails with the harness's own error message; it is never retried in that run.

---

## Harness facts

`xcheck doctor` checks flag names in the installed CLI's `--help` and the
Grok/Agy model listings. Tests check command construction and captured stream
shapes; neither proves a live tool invocation. Re-verify changed adapters after
a CLI upgrade with a small complete packet and a capacity-eligible lane.
The unverified surfaces are listed under Current limits.

All lanes receive the same packet and provenance-first instruction to read
`PACKET.md` then `PROMPT.md`. Codex, Claude, and Agy are instructed to use
reading tools only (including connected MCP tools); Grok may use Bash and
disposable scratch files inside its workspace but must leave staged evidence
and other paths untouched. Stream lanes print the answer as their final
message; TUI lanes write `ANSWER.md`. In packet mode the message forbids web
tools for every lane, including MCP web tools; the MCP limit is instruction-only.
"Connected" means that harness's own active server set, which can differ from
the other harnesses. A live Grok lane called qmd, and live Codex and Agy lanes
called semble without an approval prompt; Claude's MCP path is awaiting a
capacity-eligible smoke.

**Codex** (`codex exec`): `--skip-git-repo-check --ephemeral
-s read-only -C <work> -m <model> -c model_reasoning_effort="<e>"
-c web_search="disabled|live" -c check_for_update_on_startup=false --json -o <final.md> -` with the message on
stdin. It loads connected MCP servers from `~/.codex/config.toml`;
`-o` writes the clean final message. Web is the `web_search` config key; `exec`
has no `--search`. Errors: top-level `error` event and `turn.failed.error.message`.
The read-only sandbox still lets it run read commands (`cat`, `rg`) anywhere.

**Claude Code**: `claude -p --restricted --mcp-config ~/.claude.json --permission-mode dontAsk
--tools Read --allowedTools Read,mcp__* --add-dir <work> --no-session-persistence --model <m>
--effort <e> --permission-prompts none --debug-file <harness.log>
--output-format stream-json --verbose
--include-partial-messages`, message on stdin. `--restricted` ignores user
settings and hooks, so `--mcp-config` explicitly loads local MCP server
definitions; account-connected servers load separately. `--permission-prompts none` denies anything that would
prompt instead of waiting; partial messages keep the stream moving through a
long think so the idle clock only measures real silence. Never `--bare`: it
skips the keychain, so subscription auth fails. Final text is the `result` event's `result`; on
`is_error: true` that field is the error message (and `subtype` may still say
`success`), so it is never treated as an answer. Its `rate_limit_event` carries
five-hour and seven-day utilisation. `DISABLE_AUTOUPDATER=1` suppresses update
prompts. In live mode the tool lists also include `WebSearch,WebFetch`; in the
TUI they include `Write` for `ANSWER.md`.

**Grok** (stream): `grok --prompt-file <message> -m <model> --reasoning-effort <e>
--cwd <work> --tools read_file,grep,list_dir,run_terminal_cmd,web_search,web_fetch
--no-subagents --no-plan --sandbox read-only --always-approve
--debug-file <harness.log> --output-format streaming-json`. Packet mode uses
`--tools read_file,grep,list_dir,run_terminal_cmd --disable-web-search`. `GROK_MEMORY=0` and
`GROK_DISABLE_AUTOUPDATER=1` are set.

- Sandbox: `read-only` reads everywhere and kernel-denies writes outside
  `~/.grok` and tmp. `strict` cannot read `/opt/homebrew`, so the operator's
  global hooks that exec a binary (moshi-hook) fail with exit 126 and Grok
  ignores the failure; `off` removes write protection. The operator's shell
  exports `GROK_SANDBOX`, which would otherwise choose the profile.
  Re-verify: run with `--debug-file` and grep `hook failed` / `exit code 126`.
- MCP: Grok loads the operator's connected MCP servers. Its `search_tool`
  finds them and `use_tool` may invoke them without a confirmation prompt.
  The reviewer instruction forbids external actions, but the CLI does not
  enforce that boundary.
- `--no-memory` is accepted but absent from `grok --help`; the documented
  switch used here is `GROK_MEMORY=0`.
- `--tools` is honoured only headless; the interactive TUI ignores it
  (user-guide 14-headless-mode). `--always-approve` gives Bash no confirmation
  prompt in either view. The OS `read-only` sandbox blocks writes outside
  `~/.grok` and tmp; it still permits writes inside tmp, including other
  lanes. The lane instruction allows disposable writes only within its own
  workspace and forbids changing staged evidence; that boundary is
  instruction-only. Bash and the tmp/outside-tmp write boundary have been
  verified live; both adapter views completed without an approval prompt.
- Final text is the last model message after the last `tool_call`; a `usage`
  event closes each message. Narration from earlier messages is never glued
  onto the answer. Errors are `{"type":"error","message":…}`.
- Docs: `~/.grok/docs/user-guide/` (18-sandbox, 14-headless-mode, 10-hooks).

**Agy** always runs as its interactive app, even in headless placement:
`agy --sandbox --dangerously-skip-permissions --mode accept-edits --model
<selector> --log-file <harness.log> -i <message>`. The selector carries the
effort; do not pass `--effort` (fixed selectors such as the Claude Thinking
ones reject it).

- File tools reached calibration fragments throughout a 952 KB, seven-file
  packet in both Flash and Pro. The legacy stdin-bundle cap does not apply to
  this transport. That measurement proves reachability, not exhaustive reading
  or unlimited context.
- Agy starts its configured MCP servers and has a generic `call_mcp_tool`;
  there is no per-run switch. Agy has no
  tool allowlist, so the message's external-action and packet-mode web limits
  are instruction-only.
- Print mode can read files but soft-denies a tool confirmation after a
  sandbox-blocked command and ends the whole turn with no answer. Its app with
  skip-permissions avoids that print-mode failure; this is why
  `adapters.APP_ONLY` includes `agy`.
- Agy's post-turn feedback survey is disabled in its settings by
  `xcheck/trust.py`. Do not add `--disable-slash-commands`: it also disables
  plan mode.

**Herdr** (0.9.1): `pane layout --pane <id>` gives tab size and ids; `pane split
--pane <id> --direction right|down --ratio <existing share> --cwd <dir>
--no-focus` returns `.result.pane.pane_id`; `pane run <id> <command>` types a
command into the pane's shell; `pane rename`, `pane get` (tab/workspace
check), `pane close`. Address panes only by the ids a split returned, never by
title or focus.

**Environment (I13).** The orchestrating harness exports its session identity
into every child shell: Claude Code sets `CLAUDECODE`, `CLAUDE_CODE_SESSION_ID`,
`CLAUDE_CODE_CHILD_SESSION`, `CLAUDE_CODE_MESSAGING_SOCKET`/`_TOKEN`,
`CLAUDE_EFFORT`; companions add `CODEX_COMPANION_*`; Herdr adds `HERDR_*`. A
peer that inherits them is a nested session of the orchestrator. The lane
runner removes those and every `*_API_KEY` environment variable (lanes use subscription
auth), and `GROK_SANDBOX` (the adapter picks the profile). User preferences
such as `GROK_TELEMETRY_*` pass through. Re-verify: `env` in the orchestrator's
shell, then the fake-harness env test.

**Forensics and settlement.** Each lane gets its own `lanes/<lane>/harness.log`
where the CLI supports it (Agy `--log-file`, Claude and Grok `--debug-file`),
plus `stderr.log`, stream or screen, and `exit.json`. Only stdout or a stable
`ANSWER.md` supplies an answer; stderr does not reset the idle clock. The
harness inherits its quota-pool lock, so an orphan can still spend only while
that pool remains held. Termination uses bounded TERM→KILL waits for the
leader, followed by a group sweep with up to 3 seconds of grace. macOS can return EPERM for
`killpg` when a leader has exited but has not been reaped or another group
member cannot be signalled; `files.signal_group` falls back to the leader.
TUI cleanup also terminates remaining group members before releasing the pool lock.
The `run.epoch` watchdog starts at launch; its termination is recorded as a
`timeout`, and `wait` takes the supervisor lock
when it takes over after a dead supervisor. A `wait --timeout` exit 3 prints
the current status table. Process-table inspection is used only to verify an
orphan harness's identity before stopping it; if that identity is uncertain,
the harness is left alone.

**Codex as orchestrator.** Its sandbox can deny process-table access (`ps`).
Liveness therefore uses kernel locks (I14). The installed absolute-path allow
rule runs the supervisor outside that sandbox, so ordinary operation does not
depend on mid-run escalation. Do not substitute the writable vault script.

**Operator rules reach peers.** Codex lanes load `~/.codex/AGENTS.md`, and Grok lanes
load `~/.agents/rules` through `extra_rule_dirs` in `~/.grok/config.toml` (a
`GROK_CONFIG` overlay does not clear it). Relocating `CODEX_HOME` / `GROK_HOME`
would split the auth files and risk rotated credentials. The lane message
therefore states the staged review task directly; operator-level rules may
still apply to peers.
Re-verify: ask a low-effort lane to quote any global instructions it was given.

**Independence limit.** Local commands and connected MCP tools may read outside
the peer workspace, including another lane's result. Separate copies and the
review instruction prevent deliberate sharing; they do not provide adversarial
isolation. For blind two-phase work, do not stage another peer's answer until
the independent phase has finished.

---

## Merge

1. Re-read `STATUS.json` and every `RESULT-*.md` (including `invalid` ones)
   right before merging.
2. For every concrete claim, check its **premises** against the real tree
   before its reasoning. The ratifying lane fails at the premise: it can reject
   a correct fix because it misread code it was shown.
3. Never tally reviewers. Agreement counts only when premises were derived
   independently; same-family lanes (e.g. `agy-<third-party-model>` with a Claude
   orchestrator, or two Claude lanes) are correlated evidence. Volume and table count are not quality; a small delta is not
   a reason to drop the expensive lane.
4. Absence-of-code, wrong-path, and packet-claim findings are packet suspects:
   open the real file first.
5. Judge each proposed repair separately from its diagnosis. Its verification
   must be able to fail while the defect is present; two methods that read the
   same state are one signal.
6. Write `CONSENSUS.md`: one decision line; confirmed / rejected / uncertain
   table with lane provenance; for each confirmed finding the selected repair
   and its verification; lanes with no usable answer, never treated as
   disagreement.
7. Review applied fixes in a new round scoped to the delta and its affected
   contracts. Include whole relevant files, not another copy of all historical
   run debris. Stop when no actionable finding remains beyond explicitly
   accepted trade-offs. If every peer failed, say the review is unverified;
   repeated launches of an unchanged packet are not progress.

Artifacts stay in `/tmp`. Fixes go into the reviewed project with a test;
protocol lessons go into this note; run directories are never copied into the
vault.

---

## Current limits

- The roster is fixed when a BASE launches. Add a reviewer in a fresh BASE.
- A static account-verification splash can hit the 180 s TUI idle limit. The
  last screen is saved, but the process is stopped; no prompt is resent.
- Only a stream final message or `ANSWER.md` is an answer. Other scratch files
  are not scouted. A peer that writes its full review under another name can
  finish as `failed`. Inspect its saved screen/stream and, before reuse, its
  workspace; arbitrary scratch is not retained when that lane is restaged.
- `--cap` ends a lane; there is no late harvest or automatic retry. A fresh
  BASE is required to rerun it.
- Connected MCP tools are available without per-call review. The instruction
  against external actions is not a tool-level control. `--web packet` is not
  suitable as a security boundary for untrusted material in this setup.
- Claude stream and TUI denial probes, and Claude MCP invocation, have not
  been run since the five-hour quota was below reserve. Real quota and auth
  errors have not been captured in fixtures. A non-answer cannot validate;
  preserve the next real error stream for diagnosis.
- If the runner dies and the process table is unreadable, `wait` cannot safely
  verify an orphan harness's identity before stopping it. It preserves that
  uncertainty in the lane reason; the inherited pool lock remains held while
  the orphan can still spend. Do not kill a recorded PID on a guess.

- Pane cleanup is attempted once per completed lane per supervisor attachment.
  An unreadable pane or uncertain tab/workspace ownership is not treated as a
  successful close. `cleanup_pending` is reported separately; a valid review
  remains `done`. Inspect the recorded pane and its ownership before acting.
- Trust updates preserve unrelated parsed configuration and are idempotent.
  A malformed config or an unsupported existing Codex table layout is refused
  without rewriting the file; repair that config explicitly rather than
  auto-approving a dialog or silently normalizing the whole file.

## Repository

`python3 -m unittest discover -s tests` and `git diff --check`.
Runtime: Python 3.11+ and the installed harness CLIs. Code is stdlib-only; the one
test-only dependency is Hypothesis (`requirements-test.txt`), imported
unguarded so a missing install fails by name instead of skipping the property
lane. `tests/test_xcheck.py` runs end-to-end against a fake harness that speaks
each CLI's stream shape, plus real captured streams in `tests/fixtures/`;
`tests/test_laws.py` holds property laws for the answer contract, stream reader
and trust updates. Tests use disposable configs and quota roots; they must not
alter the operator's real trust settings or compete with live runs.

After changing `bin/` or `xcheck/`, publish with `bin/xcheck install`, then run
`xcheck doctor`. Keep source, tests and this operating contract together in git.
Historical runs belong in `/tmp`; durable requirements and current limits belong
here, not in parallel TODO or decision-ledger files. History is `git log`.
