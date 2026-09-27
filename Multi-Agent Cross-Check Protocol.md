---
aliases: [Multi-Agent Cross-Check, Multi-Agent Cross Check Protocol, multi agent cross check, X-Check]
tags: [multi-agent, cross-check, claude-code, codex, grok, agy, herdr]
---

# Multi-Agent Cross-Check Protocol

Fan a staged review packet out to **other** agent CLIs (Codex, Claude Code,
Grok, Agy) as real processes, collect each one's answer as a file, and merge
them yourself. Two placements of the same lanes: **headless** (no windows) and
**Herdr** (one visible pane per lane, right of the orchestrator).

This folder is its own git repo. `bin/xcheck` is the only entry point; the
predecessor protocol (v1) is kept read-only in `../x-check-old/` for its
history and measurements.

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
| I3 | Peers are read-only and have no MCP; they may search the web unless `--web packet` | adapter flags (see Harness facts) |
| I4 | Every lane ends: `--idle` without output or `--cap` wall time kills its process group | lane runner |
| I5 | No silent shrinkage: every selected lane has a status row, and every non-`done` row is told to the operator in chat | `STATUS.md` + orchestrator duty |
| I6 | No spend below the capacity reserve; nothing is retried automatically | capacity preflight; no retry code exists |
| I7 | Files are the truth: the answer is the harness's final message, extracted from its structured stream and validated | lane runner + `xcheck/validate.py` |
| I8 | One launch per BASE | `BASE/.launched/` (atomic mkdir) |
| I9 | A dead orchestrator loses nothing: lanes finish on their own and write their own `exit.json` | `xcheck wait --base` |
| I10 | At most one run spends from a quota pool at a time; lanes of one run share it; disjoint pools run in parallel | per-pool `flock` held by the lane runner while it can spend (`xcheck/pools.py`) |
| I11 | Lane name is provenance: `<harness>-<exact model>[-<effort>]`, and that model is what is launched | `adapters.Lane.name` |
| I12 | The orchestrating model spends no turns while lanes work | one blocking call that prints only the final table; `--detach` + `wait --timeout S` where a tool call cannot hold that long |
| I13 | No orchestrator identity or API key reaches a peer | lane runner scrubs `CLAUDECODE`, `CLAUDE_CODE_*`, `CLAUDE_EFFORT`, `CODEX_COMPANION_*`, `HERDR_*`, provider API keys |
| I14 | Liveness is proven by the kernel, never by PIDs or the process table | each runner and supervisor holds an exclusive lock on its own `alive` file; alive = that lock is taken |

Scope: one operator, one machine (macOS), attended runs, several orchestrator
sessions possibly running cross-checks at once. A reviewer finding that needs
a reboot mid-run or an unattended multi-hour wait is out of scope unless a real
run hit it.

---

## Run it

```bash
xcheck doctor                                          # installed copy, flags, model pins
xcheck run --base /tmp/agent-offload/<slug> --orch claude   # default roster minus claude
```

`xcheck` on PATH is `~/.local/bin/xcheck`, a read-only copy published by
`bin/xcheck install` from this folder (re-run it after changing `bin/` or
`xcheck/`; `doctor` fails while the copy is stale). Run the command plainly,
with no `NAME=value` prefix, `cd … &&`, pipe, or redirect: that is the shape
the Codex allow rule matches.

1. **Stage** `BASE=/tmp/agent-offload/<slug>/` (packet contract below).
2. **Record** BASE, roster, and the exact command in the task's tracker
   (e.g. its `TODO.md`) before spending anything.
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
   | Codex | `run … --detach`, then `wait --base B --timeout S` under the call's timeout. `~/.codex/rules/xcheck.rules` (written by `install`) runs `xcheck` outside the sandbox with no approval; the rule is safe because its target is the read-only copy under `~/.local`, which the sandbox cannot write |
   | Agy | allowed; every Agy lane is dropped (`--orch agy`) |

   `wait --timeout S` exits 3 while lanes still run, so each extra return costs
   one short message. Stage and launch before loading unrelated material into
   the orchestrator: every return re-sends its whole context.
5. **Report** every non-`done` lane in the next chat message, using its
   `reason` and `stderr_tail` from `STATUS.json`:
   `⚠️ <lane> <status> — <cause in plain words>. Fix: <remedy>. Continuing with <others>.`
6. **Merge** (rules below) into `CONSENSUS.md` in BASE.

`run` options: `--lanes kind:model[:effort],…` replaces the roster;
`--mode auto|headless|herdr` (auto = Herdr when available); `--web live|packet`
(default `live`: peers may search the web and cite what they used; `packet` for
untrusted input, where a prompt-injected packet plus a fetch tool could leak
what the lane can read);
`--idle` (900 s without any stream output) / `--cap` (3600 s spend budget per
lane); `--force-lane NAME` launches past a capacity refusal (say so in chat);
`--skip-capacity` for smoke runs; `--detach`. A lane whose pool is held by
another run waits in its own process (no orchestrator turns) for up to `--cap`,
then ends `skipped`.

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

The two Gemini lanes are not interchangeable. Flash tends to ratify the
packet's claims, including false ones; Pro tends to find defects and reject
claims. Both score `done`. Keep Pro.

---

## Packet contract

```text
BASE/
  PACKET.md        provenance first, then the task; exactly one `Proof target: <BASE-relative path>`
  PROMPT.md        the answer schema; exactly one `RESULT_REQUIRE: <KEYWORD>` line
  evidence/        staged copies, repo-relative paths preserved
```

`xcheck run` refuses (exit 2, nothing spent) when either file is missing, the
proof target is absent/outside BASE/empty, or the declarations are not exactly
one each.

What `run` adds:

```text
  run.json                 lanes, contract, settings, Herdr pane ids
  lanes/<lane>/work/       the lane's cwd: copies of PACKET.md, PROMPT.md, evidence/
  lanes/<lane>/message.txt the one-paragraph instruction every lane receives
  lanes/<lane>/stream.jsonl, stderr.log, pids.json, exit.json, view.log (headless)
  RESULT-<lane>.md         the lane's final message, as printed
  STATUS.json / STATUS.md  one row per selected lane, including skipped ones
```

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

Final line, alone: RESULT_COMPLETE
If you cannot do the review, make the final line RESULT_INVALID instead.

RESULT_REQUIRE: VERDICT
```

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
titled `xcheck:<lane>` and runs the same lane runner with `pane run`, so it
shows the harness's live tool calls and text. Completion is the lane's
`exit.json`, never pane text. After a lane ends its pane lingers 3 s, then the
supervisor closes it, only if it is still in the caller's tab. The wall shows a
rendered event stream, not the vendor's interactive TUI.

What that means in practice: each pane runs the same harness (Codex, Claude,
Grok, Agy) in its non-interactive mode and shows what it does as it happens:
every file it reads, every command and search, and its text. You cannot type
into a peer mid-run; you watch it. v1 instead started the interactive app and
typed the prompt into it, which is where splash screens, trust dialogs,
surveys, composer readiness, swallowed prompts and quota modals came from (most
of v1's `herdr-host`). None of those exist here.

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

Every flag below is in the installed CLI's `--help` (checked by
`xcheck doctor`) and was exercised by a real smoke run; `tests/fixtures/` holds
the captured streams. Re-verify after a CLI upgrade: `xcheck doctor`, then a
smoke run with a three-line packet and `--lanes` at low effort.

All lanes receive the same `message.txt`: who convened the review, read
`PACKET.md` then `PROMPT.md`, create no files, print the answer as the final
message. In packet mode it also forbids web tools.

**Codex** (`codex exec`): `--skip-git-repo-check --ephemeral --ignore-user-config
-s read-only -C <work> -m <model> -c model_reasoning_effort="<e>"
-c web_search="disabled|live" --json -o <final.md> -` with the message on
stdin. `--ignore-user-config` drops `~/.codex/config.toml` (MCP, profiles);
`-o` writes the clean final message. Web is the `web_search` config key; `exec`
has no `--search`. Errors: top-level `error` event and `turn.failed.error.message`.
The read-only sandbox still lets it run read commands (`cat`, `rg`) anywhere.

**Claude Code**: `claude -p --restricted --permission-mode dontAsk
--strict-mcp-config --mcp-config '{"mcpServers":{}}' --tools Read
--allowedTools Read --add-dir <work> --no-session-persistence --model <m>
--effort <e> --permission-prompts none --output-format stream-json --verbose
--include-partial-messages`, message on stdin. `--restricted` ignores user
settings and hooks. `--permission-prompts none` denies anything that would
prompt instead of waiting; partial messages keep the stream moving through a
long think so the idle clock only measures real silence. Never `--bare`: it
skips the keychain, so subscription auth fails. Final text is the `result` event's `result`; on
`is_error: true` that field is the error message (and `subtype` may still say
`success`), so it is never treated as an answer. Its `rate_limit_event` carries
five-hour and seven-day utilisation.

**Grok**: `grok --prompt-file <message> -m <model> --reasoning-effort <e>
--cwd <work> --tools read_file --deny 'MCPTool(*)' --no-subagents --no-plan
--no-auto-update --sandbox read-only --always-approve --output-format
streaming-json --disable-web-search`, with `GROK_MEMORY=0` set.

- Sandbox: `read-only` reads everywhere and kernel-denies writes outside
  `~/.grok` and tmp. `strict` cannot read `/opt/homebrew`, so the operator's
  global hooks that exec a binary (moshi-hook) fail with exit 126 and Grok
  ignores the failure; `off` removes write protection. The operator's shell
  exports `GROK_SANDBOX`, which would otherwise choose the profile.
  Re-verify: run with `--debug-file` and grep `hook failed` / `exit code 126`.
- MCP: Grok still starts the operator's MCP servers and lists their tools via
  `search_tool`; `--deny 'MCPTool(*)'` makes every `use_tool` call fail with
  "Denied by permission policy". Keep the deny.
- `--no-memory` is accepted but absent from `grok --help`; the documented
  switch is `GROK_MEMORY=0`, which v2 uses.
- `--tools` is honoured only headless; the interactive TUI ignores it
  (user-guide 14-headless-mode), which is one reason v2 never drives the TUI.
- Final text is the last model message after the last `tool_call`; a `usage`
  event closes each message. Narration from earlier messages is never glued
  onto the answer (v1 lost reviews to exactly that in `--output-format json`'s
  `.text`). Errors are `{"type":"error","message":…}`.
- Docs: `~/.grok/docs/user-guide/` (18-sandbox, 14-headless-mode, 10-hooks).

**Agy**: `agy --sandbox --mode plan --model <selector> --output-format
stream-json --print-timeout 0s --log-file <lane>/agy-cli.log`, message on stdin
(no `-p`; `-p` makes it ignore stdin). The selector carries the effort; do not
pass `--effort` (the flag exists, but fixed selectors such as the Claude Thinking
ones reject it).

- Agy 1.2.12 print mode completes turns that call file tools (paged
  `view_file`, `run_command` reads), so it reads the packet like every other
  lane. Measured with `tools/read-depth-probe`: Flash and Pro reached every
  calibration fragment at every depth of a 952 KB, seven-file packet. v1's
  192,000-byte cap limited its stdin bundle, a transport v2 does not use.
- Agy starts the operator's MCP servers (a `pond mcp` child was observed)
  and has a generic `call_mcp_tool`; there is no per-run switch. A call that
  needs permission is denied and empties the answer, so the message limits
  lanes to file-reading tools.
- A permission-requiring tool (any file write) is auto-denied in headless mode
  and the **whole answer is lost**: `result.response` is empty and stderr says
  `jetski: no output produced … auto-denied`. The lane scores `failed`; the
  message forbids writes to prevent it.
- `--disable-slash-commands` disables plan mode ("--mode plan has no effect");
  do not add it.
- Agy has no tool allowlist: web and MCP tools exist in plan mode. Packet-mode
  web-off is instruction-only for Agy.
- `result.status` can be `ERROR` while `response` holds a complete answer
  after an internal retry (API 500). The runner validates the body, so such a
  lane is `done` with the error as a note.

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
runner removes those, every provider API key (all lanes use subscription
auth), and `GROK_SANDBOX` (the adapter picks the profile). User preferences
such as `GROK_TELEMETRY_*` pass through. Re-verify: `env` in the orchestrator's
shell, then the fake-harness env test.

**Codex as orchestrator.** Measured under this machine's Codex config: its
`workspace-write` sandbox allowed the lanes' network and keychain reads but
denied reading the process table (`ps`). v2 therefore never needs `ps` for
liveness (I14), and the allow rule removes the sandbox from the run entirely,
so Codex never has to escalate mid-run.

**Operator rules reach peers.** Codex lanes load `~/.codex/AGENTS.md` (even
with `--ignore-user-config` and `-c project_doc_max_bytes=0`), and Grok lanes
load `~/.agents/rules` through `extra_rule_dirs` in `~/.grok/config.toml` (a
`GROK_CONFIG` overlay does not clear it). Relocating `CODEX_HOME` / `GROK_HOME`
would split the auth files and risk rotated credentials. The lane message
therefore scopes standing user-level instructions out of the review.
Re-verify: ask a low-effort lane to quote any global instructions it was given.

**Independence limit.** Codex and Agy can run read commands, so they can read
paths outside their workspace, including another lane's `RESULT-*.md` if it
finished first. The message tells them to stay in their directory; nothing
enforces it. For blind two-phase work, keep the material a lane must not see
out of BASE until that phase.

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
7. Review the applied fixes in a new round. Stop when a round returns only
   trade-offs you have consciously accepted.

Artifacts stay in `/tmp`. Fixes go into the reviewed project with a test;
protocol lessons go into this note; run directories are never copied into the
vault.

---

## Not in v2 (add only when a real run needs it)

- Perplexity search lane (v1 had `pwm-packet` wiring).
- Reboot detection, late harvest after a deadline, detached cleanup workers,
  pane-screen classification: v1 machinery for scenarios outside this scope or
  created by driving interactive TUIs.

## Repository

`python3 -m unittest discover -s tests`. Code is stdlib-only; the one
test-only dependency is Hypothesis (`requirements-test.txt`), imported
unguarded so a missing install fails by name instead of skipping the property
lane. `tests/test_xcheck.py` runs end-to-end against a fake harness that speaks
each CLI's stream shape, plus real captured streams in `tests/fixtures/`;
`tests/test_laws.py` holds the property laws for the answer contract and the
stream reader. History is `git log`.
