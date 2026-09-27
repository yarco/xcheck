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
| I3 | Peers are read-only, have no MCP, and no web unless `--web live` | adapter flags (see Harness facts) |
| I4 | Every lane ends: `--idle` without output or `--cap` wall time kills its process group | lane runner |
| I5 | No silent shrinkage: every selected lane has a status row, and every non-`done` row is told to the operator in chat | `STATUS.md` + orchestrator duty |
| I6 | No spend below the capacity reserve; nothing is retried automatically | capacity preflight; no retry code exists |
| I7 | Files are the truth: the answer is the harness's final message, extracted from its structured stream and validated | lane runner + `xcheck/validate.py` |
| I8 | One launch per BASE | `BASE/.launched/` (atomic mkdir) |
| I9 | A dead orchestrator loses nothing: lanes finish on their own and write their own `exit.json` | `xcheck wait --base` |
| I10 | One cross-check spends quota at a time on this machine | global flock + refusal while any recorded lane is alive |
| I11 | Lane name is provenance: `<harness>-<exact model>[-<effort>]`, and that model is what is launched | `adapters.Lane.name` |

Scope: one operator, one machine (macOS), attended runs. A reviewer finding
that needs a second concurrent run, a reboot mid-run, or an unattended
multi-hour wait is out of scope unless a real run hit it.

---

## Run it

```bash
X="$HOME/.local/bin/xcheck"
"$X" doctor                                   # flags + model pins vs installed CLIs
"$X" run --base /tmp/agent-offload/<slug> --orch claude   # default roster minus claude
```

1. **Stage** `BASE=/tmp/agent-offload/<slug>/` (packet contract below).
2. **Record** BASE, roster, and the exact command in the task's tracker
   (e.g. its `TODO.md`) before spending anything.
3. **Announce** the roster in chat: `Fan-out: N peers → <lanes>. Not peers: <harness> (<why>)`.
4. **Run** `xcheck run`. It blocks until every lane has ended and prints the
   status table. A lane can take up to `--cap` (default 2700 s), so run it as a
   background shell job and wait for its completion notice; do not poll panes
   or files while it runs. Harnesses whose foreground calls are capped (Claude
   Code 10 min, opencode 10 min, Grok 2 min) must use their background mode.
5. **Report** every non-`done` lane in the next chat message, using its
   `reason` and `stderr_tail` from `STATUS.json`:
   `⚠️ <lane> <status> — <cause in plain words>. Fix: <remedy>. Continuing with <others>.`
6. **Merge** (rules below) into `CONSENSUS.md` in BASE.

`run` options: `--lanes kind:model[:effort],…` replaces the roster;
`--mode auto|headless|herdr` (auto = Herdr when available); `--web packet|live`;
`--idle` (600) / `--cap` (2700) seconds; `--force-lane NAME` launches past a
capacity refusal (say so in chat); `--skip-capacity` for smoke runs.

Other commands: `xcheck status --base B` (render from files),
`xcheck wait --base B` (reattach after the orchestrator died).

Exit codes: 0 finished (read the table), 2 refused before any spend
(packet, lock, launch-once, orchestrator rule), 130 supervisor interrupted.

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
lane name changes with them.

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
  (`MANIFEST_TAIL_TOKEN: <random hex>`), and stage it last.
- **Say what a follow-up round is.** When reviewing fixes from an earlier
  round, stage the delta plus what it touches, name what earlier rounds
  settled, and say the code is a response to a review.

### Answer schema (template for PROMPT.md)

```text
1) VERDICT: a decision and its single most important reason, ≤80 words.
2) Findings. For each: severity; evidence (file, symbol, behaviour); root cause;
   how often the scenario occurs and what ignoring it costs; the smallest repair
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
`6) PROOF` followed by the token on the next line; anything else after
`PROOF:` is compared as the token and fails.

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
rendered event stream, not the vendor's interactive TUI: no TUI is driven, so
no splash, trust dialog, composer, or overlay can stall a lane.

If the supervisor dies in Herdr mode the panes keep running and finish on
their own. `xcheck wait --base B` reattaches, waits, renders status and closes
the panes. In headless mode a supervisor signal stops the lanes (`interrupted`).

---

## Capacity

`bin/agent-capacity` (carried from v1) reads each pool's free subscription
meter without spending a prompt: Codex app-server rate limits, Claude OAuth
usage, Grok billing (weekly credits view), Agy's local quota summary (per
family). `run` probes each selected pool once. A lane launches only on
`eligible`; the reserve is 10 %, and for Agy families it is `5 + 5n` % for `n`
lanes from that family (capped at 90). `reserve-low`, `auth-required`, and
`capacity-unknown` are skipped and must be announced.

Claude's `security` exit 36 in the probe means a locked login keychain: run
`keychain-unlock-gui` (operator at the screen), or pass a manual reading via
`AGENT_CAPACITY_CLAUDE_MANUAL_REMAINING` + `…_CAPTURED_AT` (≤5 min old).

Capacity is a snapshot. A lane that hits its cap mid-run fails with the
harness's own error message.

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
--effort <e> --output-format stream-json --verbose`, message on stdin,
`ANTHROPIC_API_KEY` removed (subscription auth). `--restricted` ignores user
settings and hooks. Final text is the `result` event's `result`; on
`is_error: true` that field is the error message (and `subtype` may still say
`success`), so it is never treated as an answer. Its `rate_limit_event` carries
five-hour and seven-day utilisation.

**Grok**: `grok --prompt-file <message> -m <model> --reasoning-effort <e>
--cwd <work> --tools read_file --deny 'MCPTool(*)' --no-subagents --no-plan
--no-auto-update --sandbox read-only --always-approve --output-format
streaming-json --disable-web-search`, with `GROK_MEMORY=0` set and
`GROK_SANDBOX` removed from the environment.

- Sandbox: `read-only` reads everywhere and kernel-denies writes outside
  `~/.grok` and tmp. `strict` cannot read `/opt/homebrew`, so the operator's
  global hooks that exec a binary (moshi-hook) fail with exit 126 and Grok
  ignores the failure; `off` removes write protection. The operator's shell
  exports `GROK_SANDBOX`, which would otherwise choose the profile.
  Re-verify: run with `--debug-file` and grep `hook failed` / `exit code 126`.
- MCP: Grok still starts the operator's MCP servers and lists their tools via
  `search_tool`; `--deny 'MCPTool(*)'` makes every `use_tool` call fail with
  "Denied by permission policy". Keep the deny.
- `--no-memory` is not a Grok flag; memory is `GROK_MEMORY=0`.
- Final text is the `text` chunks after the last `tool_call`; errors are
  `{"type":"error","message":…}`.
- Docs: `~/.grok/docs/user-guide/` (18-sandbox, 14-headless-mode, 10-hooks).

**Agy**: `agy --sandbox --mode plan --model <selector> --output-format
stream-json --print-timeout 0s --log-file <lane>/agy-cli.log`, message on stdin
(no `-p`; `-p` makes it ignore stdin). The selector carries the effort; do not
pass `--effort`.

- Agy 1.2.12 print mode completes turns that call file tools (paged
  `view_file`, `run_command` reads), so it reads the packet like every other
  lane. (Older agy exited after a tool call; that is why v1 inlined evidence
  into a 192,000-byte stdin bundle.)
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
check), `pane close`. The lane runner strips `HERDR_*` from the harness
environment so no peer can drive the operator's session.

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
   independently. Volume and table count are not quality; a small delta is not
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
- A Codex-sandboxed or Grok-sandboxed orchestrator (v1's installed runtime and
  `~/.codex/rules/xcheck.rules`). From Claude Code or opencode this is not
  needed; from inside Grok, use Herdr mode (panes run outside its sandbox).
- `--web live` beyond flag plumbing.
- Cross-run parallelism, per-pool admission, reboot detection, late harvest,
  detached cleanup workers, pane-screen classification: v1 machinery for
  scenarios outside this scope or created by driving interactive TUIs.

## Repository

`python3 -m unittest discover -s tests` (stdlib only; fake harness for
end-to-end runs, real stream fixtures for parsers). History is `git log`.
