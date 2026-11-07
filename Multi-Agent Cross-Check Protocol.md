---
aliases: [Multi-Agent Cross-Check, Multi-Agent Cross Check Protocol, multi agent cross check, X-Check]
tags: [multi-agent, cross-check, claude-code, codex, grok, agy, acpx, herdr]
---

# Multi-Agent Cross-Check Protocol

X-Check fans one immutable review packet out to **other** coding-agent CLIs as real processes, collects one answer file per lane, and leaves judgment to the orchestrating model. Codex, Claude and Cline run their own first-party headless streams and every other lane is an ACP session driven by acpx; one lane runner serves two placements: **headless** (child processes) and **Herdr** (one visible pane per lane, right of the orchestrator, showing that lane's stream).

| Owner | Holds |
|---|---|
| This note | review procedure, packet contract, invariants, limits and merge rules |
| `bin/xcheck` + `xcheck/` | everything with one mechanical right answer: exact argv, process groups, deadlines, capacity, locks, placement, stream parsing, validation, status |
| The orchestrator | judgment: roster, packet, diagnosing a failed lane and telling the operator, premise checks, adjudication, `CONSENSUS.md` |

The code never judges a finding and never retries a reviewer turn; the model never improvises process control. Native launch flags live in `xcheck/adapters.py` and `xcheck/cline.py`; the [technical reference](docs/reference.md) records why non-obvious controls exist. Read its relevant sections for native diagnostics, qualification and implementation changes; routine reviews use this note. [Runtime architecture](docs/architecture.md) maps requirements to owners, regression evidence and retirement conditions. Simplification must preserve those requirements and their motivating insights; reducing line count is not an acceptance criterion.

Scope: one operator, one macOS machine, attended runs, possibly several orchestrator sessions running cross-checks at once. This public contract describes the runner and review method. Machine-local settings and optional companion automation are separate; see [Adoption](docs/adoption.md). Reboot mid-run, adversarial peers, and unattended multi-hour recovery are outside the contract.

## Preconditions and source authority

Before launch, use the installed runtime matching the source (`doctor`), the real orchestrator kind, a fresh BASE under `/tmp/agent-offload`, and a valid whole-file packet. Selected peers need their native CLI/server and existing subscription or provider login. Check provider data terms before sharing the packet. These are readiness conditions, not permission to repair accounts, enable billing, lower effort or replace a model automatically. A doctor failure is a named coverage gap; report it and use lane admission to distinguish a blocked selected lane from healthy peers.

The host must retain a wait or provide a verified completion callback to promise automatic return. Without either, detach and report collection pending. Grok orchestration additionally requires verified Herdr placement; other orchestrators use auto placement. Applicable native startup/model readiness and capacity checks run again at lane admission; `doctor` separately checks the whole pinned roster. A previous smoke, doctor result or successful review is not present admission evidence.

| Question | Authority and refresh rule |
|---|---|
| What is selected? | `adapters.py` defines defaults and the Devin pin; optional OpenCode/Cline lanes come from the configured local free-roster file (default `~/.local/state/xcheck/free-roster.json`); `BASE/run.json` records this run's exact selectors, efforts and settings. Default-roster edits are deliberate |
| What can launch now? | Fresh native startup/model inventory and capacity checks under admission; failed reads mean unknown, not absent |
| What is free now? | Live provider prices and served IDs from `xcheck models`; no saved-price fallback. A free roster records dated lane-check results, never authoritative prices. Native client cache is advisory support metadata only |
| What did peers review? | `BASE/.packet`, captured at launch; later edits to inputs or reused lane workspaces cannot revise it |
| Is a lane live or complete? | Kernel liveness locks and the authoritative lane `exit.json`; status is derived, and native progress, PIDs and pane text are diagnostic |
| What did the review establish? | Settled native streams/results plus the host's premise checks in `CONSENSUS.md`; record scope, limitations and dated evidence |

Keep durable instructions in this Markdown note and version-specific integration documentation. Date availability/price observations and name the provider or CLI version; never read “latest”, “free” or “working” as a permanent guarantee. Recheck after upgrades and before qualification. Captured fixtures are regression inputs, not live inventories. Store private run evidence under BASE, not in the source checkout; a past review must not become a new packet's current-state claim.

## Run it

```bash
xcheck doctor                                              # installed copy, flags, model pins
xcheck run --base /tmp/agent-offload/<slug> --orch claude  # default roster minus the orchestrator's harness
```

`xcheck` on PATH is `~/.local/bin/xcheck`, a read-only, content-addressed copy that `bin/xcheck install` publishes from this repo. `doctor` compares it with the source digest even when invoked through the copy. Reinstall after changing `bin/` or `xcheck/`.

1. **Stage** `BASE=/tmp/agent-offload/<slug>/` (Packet below). Stage and launch before loading unrelated material: every later return re-sends the whole context.
2. **Record** BASE, roster and the exact command in the existing task tracker or chat before spending. `run.json` persists the settings; write no separate handoff for a run.
3. **Announce**: `Fan-out: N peers → <lanes>. Not peers: <harness> (<why>)`.
4. **Run** without spending master-model turns: retain one blocking host operation or background job with a real completion callback. The local supervisor waits without inference. Do not schedule repeated short waits or model health prompts. Progress inspection may read only `xcheck activity` metadata; keep active findings, tool outputs and answer drafts unseen until settlement. Host notifications, repeated tool returns and large metadata outputs are not evidence of zero token use. A local wait invokes no model; subscription savings require a retained operation or verified completion callback without periodic model resumption.

   | Orchestrator | Form |
   |---|---|
   | Claude Code | `xcheck run …` as a background shell job; its completion notice is the one return |
   | Codex | absolute `~/.local/bin/xcheck run …` in a retained host shell operation, or `--detach` followed by one retained `xcheck wait --base B`. Keep shell polling inside the host tool, without returning to model sampling. The allow rule covers the installed absolute path followed by `run`, `wait`, `status`, `activity`, `cancel`, `doctor`, `capacity` or `models`; never point it at the source checkout |
   | Grok | Herdr mode only. Background `xcheck run`, then retain its host completion operation; reaching a host wait cap detaches the waiter, never the reviewer |
   | opencode | In a persistent V2 TUI/web/desktop session, launch `xcheck run … --detach`, then invoke one `xcheck wait --base B` through the native `shell` tool with `background=true`. Completion inserts the shell output into the session and automatically resumes the host (verified on 2.0.27, with no polling). A one-shot `opencode run --standalone` exits at its first idle response and cannot retain this callback; use a retained foreground wait there or disclose BASE pending. `--orch opencode` drops opencode lanes |
   | pi | retain a host background completion job if available; otherwise detach and report BASE pending. Never pi's own delegate/subagent tools as peers |
   | Agy | retain a host completion job; `--orch agy` drops every Agy lane |
   | Devin | retain a host completion job; `--orch devin` drops Devin lanes |
   | Cline | retain a host completion job if available; otherwise detach and report BASE pending. `--orch cline` drops Cline lanes |
   | anything else | `--orch other`; one retained wait/completion callback. Without that capability, detach and disclose pending collection. Never claim a different harness to bypass exclusion |

   `wait --timeout S` is for an explicit bounded status request. Exit 3 means still pending, and authorizes neither reviewer cancellation nor another scheduled master turn. `.done` is a durable completion record, not a host notification transport. Verify the host callback or retained-operation behavior; do not claim automatic resumption from a file write alone.

5. **Report** every non-`done` lane in the next chat message from final `STATUS.json` (`reason`, `stderr_tail`); during a pending run use each settled `lanes/<lane>/exit.json`, since shared summaries are published only when supervision stops: `<lane> <status> — <cause in plain words>. Fix: <remedy>. Continuing with <others>.` Also report MCP startup warnings and retrieval-policy diagnostics, including those on `done` lanes. Native completion and proof do not certify policy compliance; a successful unapproved call does not establish a charge.
6. **Merge** (below) into `BASE/CONSENSUS.md`.

`run` options: `--lanes kind:model[:effort],…` replaces the roster (Agy and Devin take no effort; OpenCode's ACP effort and Cline's thinking level are optional; Codex, Claude and Grok require one); `--add LANE` appends an opt-in lane to it. `--mode auto|headless|herdr` (default `auto`: visible Herdr panes when the caller verifies, else headless). Leave it unset; `--mode headless` is for an operator request or a placement smoke, never an orchestrator's preference, because it hides lanes the operator expects to watch. `--web live` (default: peers may search and must cite what they relied on) or `packet` (built-in web tools off and peers told not to fetch; connected MCP tools remain, so this is not isolation). `--idle` and `--cap` are optional operator-requested cancellation limits; neither has a default. The caller must not invent a limit to guess how long a reviewer should take. Without a limit, a quiet lane and a queued lane remain pending until completion, an observed failure or explicit cancellation. A supplied `--cap` also bounds pool admission and arms the runner's watchdog; its expiry means policy stopped the work, never that the reviewer was idle. Silence cannot establish inactivity. `--force-lane NAME` launches a selected lane past a capacity refusal, only on the operator's say-so and announced in chat. `--skip-capacity` is for controlled smokes. `--detach` returns once launch is confirmed; if preflight outlasts its 30-second observation window, it reports a live supervisor with launch unconfirmed. A reused BASE is refused before spawning another supervisor.

Invalid options (a BASE that does not resolve under `/tmp/agent-offload`, no peers, non-positive budgets, a forced lane not in the roster, a same-kind peer) are refused before any pane, model or launch marker exists. Inside that root any slug works (spaces, quotes, non-ASCII, nesting, a relative path or a symlink that resolves there), and peers always run in the fixed `.lanes/<pool>/<lane>` workspaces, so no BASE spelling reaches a trust or approval prompt. Other commands: `xcheck wait --base B [--timeout S]` (subscriber; reattaches without relaunch), `xcheck status --base B` (render from files), `xcheck activity --base B [--lane NAME] [--json]` (one compact line per lane; `--json` requests full metadata without peer findings), `xcheck cancel --base B [--lane NAME] --reason TEXT` (prevalidate all selected runner identities before signalling; preserve the operator’s reason in final status). Caller or supervisor interruption detaches; it never cancels peers.

Exit codes: 0 every lane settled (read the table); 2 refused before any spend; 3 `wait` timed out while lanes run; 130 supervisor interrupted.

## Roster

| Lane | Pool |
|---|---|
| `codex-gpt-6.1-sol-high` | codex |
| `claude-opus-5-5-high` | claude |
| `grok-4.7-xhigh` | grok |
| `agy-gemini-3.8-flash` | agy-gemini |
| `agy-gemini-4-argon` | agy-gemini |

The orchestrator's harness is excluded. `xcheck/adapters.py` owns default pins;
`run.json` records exact selectors and efforts, not merely normalized lane names.
Check current native inventory and access: a pin is not an availability promise.
An absent exact model skips before inference; an unreadable inventory is unknown.
Never substitute a model, lower effort or enable a paid route to repair admission.

Devin (`--add devin:swe-2-max`), OpenCode and Cline are opt-in. `--add` selects an
extra lane; `--lanes` replaces the roster. A configured local free-roster file can
supply optional selectors, but xcheck installs no producer or scheduler. Live
`xcheck models` discovery checks provider-owned prices and served IDs without
inference; it does not certify tool use or review quality. Trial credits are not
standing free access. Check data terms before sharing any packet.

[Model discovery and selector reference](docs/reference.md#roster) covers source
formats, free eligibility, exact effort syntax, native version observations and
provider distinctions. Prefer a small set of qualified, distinct reviewers over
launching every available offer; same-family routes remain correlated.

### Cline free models

Cline uses its native headless stream, not ACP, and remains opt-in. It requires
native login and exact free-route admission; it has no non-inference quota meter.
See [Cline discovery and completion](docs/reference.md#cline-free-models).

### Additional free providers through OpenCode

Kilo uses the native OpenCode ACP lane with an isolated store and no credential.
It is opt-in and is rechecked for free eligibility before launch. Other providers
retain their own access requirements. See [provider and optional-roster contracts](docs/reference.md#additional-free-providers-through-opencode).

## Routine review planning and qualification

A routine code review uses Codex, Claude, Grok, Gemini Flash and Gemini Argon, excluding its orchestrator. Add Devin explicitly when another independent code reviewer is wanted and its selected offer and access are verified. xcheck reads no Devin quota (its daily and weekly plan windows show only in Herdr's Agent Usage sidebar and the Devin header), so it remains opt-in. Keep all supported adapters and the free roster available; routine roster membership is a review-quality decision, separate from endpoint availability. Keep OpenCode in use when its models fit the task: select one or two qualified models for a focused question rather than launching every free offer on the same broad audit. Stage the complete relevant files, focused delta and existing test results; use packet mode when no current external evidence is needed, and ask for targeted checks only. One review prompt can cause many inference requests as the agent reads and uses tools; a successful maintenance ping does not reserve capacity for that review. A model-specific 429 is a coverage gap for that lane, not evidence that every OpenCode model should be dropped. Preserve each selected model's pinned/default effort; do not lower it to repair output formatting.

Run the required suite once on the capable host and stage its exact command, result and environment limitations with the complete source. Peers review the focused delta and relevant contracts, with the whole tree available. A peer may run a targeted reproduction for a concrete finding. It must not repeatedly run the full suite, repair its sandbox, or edit the reviewed implementation. An environment-blocked check is a limitation, not a code defect; record it and finish the review. A comprehensive scope is an explicit task choice, not a reason to keep investigating after the requested evidence is established.

A qualification records four separate outcomes: endpoint/tool access, native final-answer completion, output-contract compliance, and useful verified review evidence with elapsed time. Preserve the packet, exact selector/effort, harness versions/settings, raw stream, exit and operator interventions under its BASE so the outcome can be re-examined. Before attributing failure to a model, check packet completeness/provenance/scope, prompt clarity, routing and permissions, native tool results, transport/completion events, and result capture; replay a settled stream through the reader when capture is in doubt. A tool completed before silence does not locate the later stall. If evidence cannot distinguish the model, provider or harness, record the cause as unknown. A fresh task should distinguish a stated hypothesis or supply missing useful-review evidence; change one relevant condition where practical, and report confounders when several changed. Do not infer a cause from a successful rerun alone. A smoke proves access and framing, not suitability for a large audit. Auth, capacity, HTTP 429/503 and a failed inventory read are temporary access states unless successful provider evidence establishes otherwise. A model is absent only after a successful inventory check omits its exact selector. Repeated wrong findings or excessive cost relative to added evidence affect routine roster membership; they do not justify deleting a working adapter. Silence is pending unless an explicit owner limit or cancellation settles it.

Report a malformed completed answer as invalid and adjudicate its concrete claims as leads. Do not rerun a complete code audit solely to replace a missing newline or marker. Fix the schema instructions for the next task: show the exact two-line proof/marker ending and require a final assistant message after all tools. Never promote a draft or reasoning-only turn, normalize findings, or erase the original stream/status. Only relaunch when a changed task or repaired launch condition can provide evidence that is actually missing.

There is no automatic routine timeout. After one hour the supervisor prints one diagnostic warning with pending lanes, runner liveness, queue/running phase, stream event/tool counts and native tool state where available. This warning never settles or cancels a lane, and changed metadata never proves useful progress. Use it to investigate locks, blocked tools and repeated verification. An explicitly requested --cap remains available and separately bounds pool admission and spend; native inventory subprocesses have their own bounded reads; without an explicit cap there is no overall startup deadline. No cap is inferred from silence. Slow optional models should not be selected routinely merely because their token price is zero. Do not start another subscription/free-model round to ratify deterministic repairs that already have failing-before/passing-after regressions.

## Packet

```text
BASE/
  PACKET.md    provenance first, then the task; exactly one `Proof target: <BASE-relative path>`
  PROMPT.md    the answer schema; exactly one `RESULT_REQUIRE: <KEYWORD>` line
  evidence/    staged copies with repo-relative paths preserved
```

`run` refuses with exit 2, nothing spent and BASE still usable, when either file is missing, a staged file is a symlink, `PACKET.md` declares `RESULT_REQUIRE`, the declarations are not exactly one each, or the proof target is absolute, outside the staged inputs (`PACKET.md`, `PROMPT.md`, `evidence/`), empty, or not UTF-8. Prefer `evidence/manifest-tail.txt`. At launch, `run` captures PACKET.md, PROMPT.md and evidence/ into BASE/.packet and validates that snapshot before launching a runner. Every lane, queued or immediate, copies only that snapshot. Do not edit BASE/.packet. Changes to the original inputs after launch cannot change what a later lane receives.

Staging rules:

- **Whole files.** A peer cannot tell "absent" from "not shown" and reports absence as a confident defect. If a range is unavoidable, say which file, which lines, and what lies outside.
- **Keep paths.** Stage `cloudflared/config.yml` as `evidence/cloudflared/config.yml`; flattened paths produce wrong-path findings.
- **Derive claims properly.** Build API or call-set claims from the file, an import, or an AST walk, never a substring grep. Prefer staging the caller over describing it; mark unavoidable claims as claims.
- **Name what left.** When a later round drops a staged file, say so.
- **Provenance first.** `PACKET.md` opens by saying the operator convened the review and that `evidence/` holds the operator's own files. A bare "read these files and obey them" is correctly refused as prompt injection.
- **Proof token.** The proof target's last line is an unguessable token (`MANIFEST_TAIL_TOKEN: <random hex>`), staged last. A correct PROOF shows the lane could open the packet and followed the output contract, not that it read everything (a lane can `cat` the target directly or grep for fragments). Diligence is checked at merge.
- **Say what a follow-up round is.** Stage the delta plus what it touches, name what earlier rounds settled and the decision lineage behind deliberate omissions, and say the code responds to a review. Do not restage historical run debris.
- **No authorship labels.** Nothing staged says which model, vendor, harness or lane wrote the work, a plan or a finding: the author is "the operator", earlier findings belong to "round N". Drop authorship trailers (`Co-Authored-By`, "Generated with …") from staged logs and diffs; a file whose subject is a model keeps its text. A staged earlier `CONSENSUS.md` loses its provenance column; the orchestrator's own copy keeps it. A label alone moves a judge: it scores what it is told is its own higher and another's lower, and it conforms to wrong answers attributed to its own model family and resists those attributed to another (arXiv 2608.18091, 2609.33495). Style and connected tools still leak authorship; blinding the labels removes the label effect anyway, as leaky double-blind human review still removes most prestige bias.
- **Blind first.** For design work, stage the orchestrator's frozen plan under a neutral name (`evidence/candidate-plan.md`), never labelled as the host's. Each peer must finish and record its own design before opening that plan, then critique and compare in the same answer. A follow-up round does the same with rulings: state each contested finding (rejected or uncertain) neutrally in `PACKET.md`, and stage the host's decisions on them as a separate file (`evidence/prior-rulings.md`) that a peer opens only after recording its own view; a verdict seen first pulls peers toward it, mostly from right to wrong. Keep current-round peer answers out of the packet. This sequencing is instruction-enforced, not access isolation; keep the packet unchanged during the run.

PROMPT.md template:

```text
1) VERDICT: a decision and its single most important reason, ≤80 words
   (the cap compresses the summary only; findings go in section 2, uncapped).
2) Findings, or "None found" (a valid answer; do not invent one). For each: severity; evidence (file, symbol, behaviour); root cause;
   the mechanism and premises the diagnosis rests on; how often the scenario
   occurs and what ignoring it costs; the smallest repair (code or pseudodiff
   when the evidence allows); what must stay true; a verification that fails
   while the defect is present; confidence and missing context.
3) What you would delete from the target, and why deleting it is safe.
4) Alternatives worth considering (if any).
5) One-line recommendation.
6) PROOF: <the last non-empty line of the proof target, copied exactly>
   Keep this on ONE plain line, without fences or annotations; put a real newline before the marker.

The next line is packet metadata; do not include it in your answer.
RESULT_REQUIRE: VERDICT
The final two lines must be separate, plain lines. Example:
6) PROOF: MANIFEST_TAIL_TOKEN: <actual token>
RESULT_COMPLETE
Final line, alone: RESULT_COMPLETE
If you cannot do the review, make the final line RESULT_INVALID instead.
```

The markers are literal lines: no period, code fence, quote or explanation after `RESULT_COMPLETE` / `RESULT_INVALID`. PROOF is `6) PROOF: <token>`, or a bare `6) PROOF` heading followed by the token alone or by a `PROOF: <token>` line (bold, bullets or backticks around either are fine); anything else after `PROOF:` is compared as the token and fails, and so does pasting the whole proof file. The last PROOF line decides, so findings may quote PROOF lines. An answer also needs review text beyond the heading, PROOF and marker. `tests/test_laws.py` states the whole grammar as properties.

**What every lane is told** (`run.MESSAGE`): the owner convened the review; read `PACKET.md` then `PROMPT.md` (PROMPT decides format); work from the staged directory; connected MCP tools are available, external actions (sending messages, changing records) are not; nobody answers questions mid-review. Codex, Claude and Agy use tools that read (plus web in live mode); Grok, OpenCode, Cline and Devin may also run Bash and write disposable scratch inside their own workspace, never the staged files. Every lane prints the answer as its final message; only native turn completion publishes it, so a complete-looking draft is not a publication signal.

**What `run` adds to BASE**: `run.json` (roster, contract, settings and any Zen account assignment fixed at launch; Herdr pane ids added as panes are created), `lanes/<lane>/` (`message.txt`, `staged` stamp, `pids.json`, `exit.json`, `stream.jsonl` + `view.log`, `stderr.log`, `harness.log` where the CLI supports one), `RESULT-<lane>.md` (the captured answer), `STATUS.json` / `STATUS.md` (one row per selected lane, skipped ones included; an assigned lane names its account, and the markdown cell is `lane (account)`). Peers work in stable workspaces `/tmp/agent-offload/.lanes/<pool>/<lane>/`, restaged only after pool admission and reused by the next run of that lane. The pool namespace makes different pools disjoint even when selectors normalize to the same lane name; inspect a run's evidence in its BASE, not in a later run's workspace. Artifacts stay under the run root; never commit run directories or private review packets.

## Lane statuses

Only the supervisor holding `supervisor.alive` publishes the shared `STATUS.json` and `STATUS.md` projections. `status` and bounded/completed `wait` derive their output from authoritative lane records without rewriting those projections. `.done` follows final summary publication. Recovery takes the lane life lock before rechecking and publishing `exit.json`, and preserves an existing terminal record. A synchronous headless runner-spawn error is recorded as failed while independent peers are attempted; uncertain Herdr delivery remains pending. These distinctions and their regression evidence are mapped in [Runtime architecture](docs/architecture.md).

| Status | Meaning | Orchestrator action |
|---|---|---|
| `done` | answer ends `RESULT_COMPLETE`, has the required heading and review text, PROOF matches; policy diagnostics are separate | read, report diagnostics and adjudicate |
| `invalid` | an answer exists but breaks the contract (reason names how), or the peer declared `RESULT_INVALID` | read it anyway; its findings are leads; tell the operator |
| `failed` | no usable final answer; reason is the harness's structured error, else its first stderr line | read `reason` + `stderr_tail`; tell the operator cause and remedy |
| `timeout` | idle limit, `--cap`, or the runner's own bound ended it | tell the operator; partial output is in `stream.jsonl` and any draft in `RESULT-<lane>.md` |
| `skipped` | no review inference: capacity reserve, auth, unknown capacity, startup/inventory unavailable, exact model absent, or pool busy past `--cap` | tell the operator; `--force-lane` only on their say-so |
| `interrupted` | a lane runner was signalled or explicitly cancelled | the final reason preserves an explicit cancellation reason; a signalled subscriber/supervisor only detaches |

Never relaunch a lane that died of quota or auth in the same run. The [pre-inference Claude authentication recovery](docs/reference.md#capacity) resumes a pending admission; it never restarts a reviewer turn. A lane lost to a bad packet or prompt is rerun in a fresh BASE after fixing the packet, and the operator is told. A lane that dies within seconds with a usage message usually means a flag drifted: read the first stderr line, run `xcheck doctor`, and compare the installed CLI's `--help` before blaming quota or the model. The only answer channel is the lane's final message; a review written to a file instead ends `failed` and is not scouted. Read its saved stream, and the file in `/tmp/agent-offload/.lanes/<pool>/<lane>/`, before the next run of that lane restages it.

## Placement and views

Auto placement uses Herdr only with a verified caller binding and adequate pane
layout; otherwise it is headless. A missing or ambiguous binding cannot establish
identity. Explicit Herdr placement refuses rather than guessing. Headless and
Herdr share the same runner, reader, settlement and validation path.

The caller subscribes to a detached supervisor. Interrupting either detaches;
it does not cancel reviewers. Reattach with `wait`; use `cancel --reason` to stop
owned work. Pane text and `view.log` are diagnostic, never completion signals.

Only native final-turn completion can publish an answer. A native failure,
explicit timeout or cancellation outranks a complete-looking draft, including
during crash recovery. Display failures must not change execution decisions.
See [placement, stream correlation and recovery details](docs/reference.md#placement-and-views).

## Capacity

Codex, Claude, Grok and Agy have non-inference meters. Each lane reads its meter
under pool custody immediately before spend. Admission requires more than 10%
remaining, or `min(90, 5 + 5n)%` for the run's `n` Agy lanes in one family. A failed
read is `capacity-unknown`; rejected login is `auth-required`; insufficient reserve
is `reserve-low`. All skip before review inference, with no fallback meter.
Devin, OpenCode and Cline have no protocol quota meter; serialization does not
establish available allowance. A reading cannot guarantee later provider access.

Only a configured local helper may rotate Agy accounts or complete attended
Claude sign-in. Claude may first make one bounded native, non-inference refresh
under the pool lock; recovery continues the same pending admission, never retries
a reviewer turn. Diagnostic commands, forced lanes and `--skip-capacity` do not
recover authentication. Do not hand-roll OAuth, force authentication failures,
log out a working session or switch to API billing.

Use `xcheck capacity` for diagnostics. Report failed admission and its remedy;
use a fresh BASE after repairing the cause. [Meter, throttle and authentication
contracts](docs/reference.md#capacity) document the native reads, recovery bounds,
locked-Keychain handling and explicitly supplied usage readings.

## Locking and liveness

Cooperating runs share host-local quota pools and stable lane workspaces. A lane
takes one pool before capacity checking, staging or spend; same-run lanes may
share it, and disjoint pools run concurrently. Inherited kernel locks retain
custody while an owned native process is alive, including after runner death.

Never delete lock files or use a test root to bypass contention. Kernel `alive`
locks establish liveness; PIDs, process tables and output activity do not. Recovery
may stop an orphan only after verifying its identity, never from a saved PID
alone. Unverifiable or uncertain startup remains explicit uncertainty.

[Locking and recovery details](docs/reference.md#locking-and-liveness) describe
custody across ACP, crash recovery, requested deadlines and the limits of process
group ownership. [Architecture](docs/architecture.md) maps these requirements to
owners, regressions and retirement conditions.

## Harness facts

Read the [native adapter contracts](docs/reference.md#harness-facts) before changing
launch flags, tool permissions, authentication custody, completion parsing or
version admissions. Exact argv lives in code; the reference records why controls
exist and what native evidence supports them. Grok also has a dedicated
[integration contract](integrations/grok-acp/README.md).

After an adapter change or relevant CLI upgrade, qualification must exercise its
actual allowed tools, forbidden operations and native completion on a small whole
packet in both supported placements. Fixtures and `doctor` alone cannot prove
live tool behavior. Qualify a changed version pin from source before reinstalling
it for other runs. Keep live records private; publish only reviewed synthetic or
sanitized regression evidence. No account or live-model call is needed for the
offline demo.

## Invariants

| # | Invariant | Enforced by |
|---|---|---|
| I1 | Peers are harnesses other than the orchestrator's; all receive the same immutable packet | `--orch` exclusion, per-lane copies, launch-once |
| I2 | Evidence is whole files with preserved paths and a proof token staged last | orchestrator staging duty; preflight validates paths/contract, not completeness; PROOF validates token access |
| I3 | No manual approval, trust click or typed prompt is part of a review turn | readiness preflight, permission flags, verified placement, prompt supplied through stdin or file |
| I4 | A settled lane has one `exit.json`; silence never settles a lane; explicit limits remain cancellation policy | lane runner |
| I5 | No silent shrinkage: every selected lane has a status row, and every non-`done` row is told to the operator | `STATUS.md` + orchestrator duty |
| I6 | Capacity is read under pool admission right before spend; xcheck never retries a reviewer turn | lane runner, `xcheck/capacity.py`; bounded metadata/meter retries are separate, native internal retries remain a documented limit |
| I7 | Only a natively completed final message is an answer; a prior termination decision cannot be erased by recovery | `adapters.Reader`, `settlement.py`, `validate.py` |
| I8 | One launch per BASE; reattachment never relaunches | `.launched`, `wait` |
| I9 | Kernel locks, never PIDs or the process table, prove a live holder | `alive` and pool `flock`s |
| I10 | One runner, one `Reader` (native for Codex, Claude and Cline; ACP for the rest) and one settlement/validation path serve every lane in both placements | `xcheck lane`, `settlement.py` |
| I11 | Exact harness, selector and requested effort are preserved; normalized labels cannot collide within a roster | `run.json`, argv, `adapters.Lane.name`, duplicate-name preflight |
| I12 | Local supervision invokes no model; the master resumes for terminal events or an explicit request | one retained host operation/verified completion callback; disclose unsupported hosts |
| I13 | Orchestrator control variables are scrubbed; provider keys are retained only for the explicitly selected OpenCode provider; the runner sets `OPENCODE_DB` for an assigned Zen store | lane environment scrub, provider allowlist and account assignment |
| I14 | A stable workspace has one owning pool; separate pools cannot overwrite it | `.lanes/<pool>/<lane>` and pool custody before staging |
| I15 | Unknown inventory or price never becomes confirmed availability/free pricing; temporary failure never silently changes defaults | admission/discovery fail closed; deliberate roster changes and orchestrator coverage reporting |
| I16 | Progress and the one-hour warning never publish an answer, cancel a lane or authorize another turn | supervisor diagnostics, native completion reader, explicit cancellation/limits |

## Current limits

- Distinct model IDs can produce the same sanitised lane name; a roster with duplicate names is refused before launch. The exact ID remains in run metadata and argv. Across runs, colliding names under different pools have different workspaces; within one pool the existing lock owns the workspace. Empty, dot and parent path segments in model selectors are refused.
- Claude and Codex register only approved local/keyless MCP servers (Codex disables every other server it would load, plugins included, and any with a stored login), and Claude blocks account connectors; packet mode omits their remote MCP retrieval servers. Grok keeps its filtered lane config; Agy, Devin and OpenCode retain instruction-only MCP/web restrictions. Shells, connected local tools and native web tools prevent a general free-only network guarantee. `--web packet` is not a security boundary for untrusted material.
- Grok and Devin’s shell can write anywhere the operator can (Grok uses an unrestricted profile; Devin’s sandbox hides the login), including another lane's scratch; their own-workspace limit is an instruction. OpenCode uses whichever executable wins PATH; xcheck adds no OS boundary. A separately maintained wrapper may still share writable temporary directories, configuration, databases and MCP caches, so assess those boundaries before admitting private evidence. Codex (read-only sandbox), Claude (no shell) and Agy (Seatbelt: workspace, lane home, Agy uv cache) cannot.
- An Agy lane's login copy is deleted when its runner settles the lane. A runner killed outright leaves the 0600 copy in `BASE/lanes/<lane>/gemini-home` until that BASE is removed. A server refresh is written only to the copy, so the canonical item keeps its original refresh token.
- Concurrent runs each assign Zen lanes from the start of `opencode_accounts`, so they can spend the same account. The external free-roster refresh spends the account its own process is signed into. The default of 5 Zen lanes per account is an estimate from 2026-10-21, not a measured request/token ceiling or a capacity guarantee. Account count cannot establish relief from shared IP or upstream limits. As checked 2026-10-24, the [OpenCode terms](https://opencode.ai/legal/terms-of-service) prohibit using multiple accounts to circumvent usage limits; legitimate separate account ownership does not establish an unlimited shared allowance. No automatic account creation or quota-circumvention mechanism is part of this integration.
- Agy lanes share `ROOT/.cache/agy-uv`, which every Agy lane may write, so one Agy lane could alter the uvx MCP packages another Agy lane runs. It never reaches the operator's uv cache.
- No Agy lane gets `fff`, `qmd`, or `keenable-free`: the first two fail ACP initialization (see Harness facts), and Keenable's startup HTTP 429 can abort the whole Agy turn. You Free remains its web retrieval server. Requalify each excluded server before restoring it; this availability exception does not authorize a paid-capable replacement.
- Grok 1.0.46 and 1.0.49 refuse `read-only` startup on symlinked container-runtime endpoints such as OrbStack's `/var/run/docker.sock`. The current adapter uses an unrestricted profile, not an OS write boundary. Do not change host container configuration to make a review launch; requalify a restrictive profile before claiming isolation.
- A turn that blocks silently (a native tool buffering output, a stuck login) remains pending unless the operator supplies `--cap`/`--idle` or cancels; silence is not evidence of inactivity.
- Display failures first occurring after terminal publication cannot amend immutable `exit.json`. Final pane output is best effort; the authoritative record remains published before that output.
- A Herdr failure after a split but before `pane run` leaves a pane with an idle shell; close it by hand. (A failed `exec` makes zsh exit, so that pane closes itself.) A placement failure known to the supervisor is settled explicitly. An unobserved runner startup remains pending; elapsed time does not prove it failed. `cancel` settles it: with no runner recorded it takes the lane's life lock (proof none runs) and writes `interrupted`; a runner that starts later finds that record under the lock and stops.
- Only an explicitly supplied `--cap` ends a lane on elapsed spend. It is a cancellation policy; rerun in a fresh BASE after recording lost work. Native session continuation cannot restore killed OS processes or deleted scratch.
- Blinding covers labels only. Style, the reasoning itself, and connected tools (pond session history, configured note indexes) can still tell a peer who wrote something or how the host ruled.
- If a runner died and `ps` is unavailable, `wait` cannot confirm the orphan's identity and leaves it alone, noting that in the reason; its pool stays held until the orphan exits. Never kill a recorded PID on a guess.
- A full roster starts many MCP servers in the same second, which can exceed Keenable's 10 requests/s limit (HTTP 429); the lane that loses the race lacks that server for its session and keeps its built-in web tools.
- Claude lanes still read managed policy and `~/.claude.json` state. Grok lanes make one initial title-sampling call per session that no supported switch disables.
- `main` carries no locally patched dependency. The ACP forms of the Codex and Claude lanes, each with its local patch, are on the `acp/codex` and `acp/claude` branches; re-check their upstream trackers (Harness facts) at each upgrade.
- Captured fixtures cover live Codex exec answers, an unsupported-model failure and an exhausted 5 h window, Codex MCP tool calls (started, completed, failed), an Agy ACP turn with the server's reused request id, a Devin ACP shell/read turn, an OpenCode ACP shell/fetch/MCP/read turn, Grok missing quota percentages, live Claude stream-json answers and a model failure, and a real successful disallowed Claude Cloudflare call. Fixtures omit credentials, response headers and account identifiers. Deployed Zen price tables and the installed V2 progress-store DDL are captured as structural fixtures; they contain no account data. Other native account quota/auth failures still need captured fixtures; invalid-model fixtures prove parser shape only.

## ACPX boundary

Stock acpx transports Grok, Agy, Devin and OpenCode. xcheck retains admission,
pool custody, snapshots, environment policy, answer selection and validation.
ACP does not standardize native sandboxes, credentials or vendor configuration;
`end_turn` alone is not a valid review. Codex, Claude and Cline use native streams.
See [transport responsibilities](docs/reference.md#acpx-boundary); never use acpx's
built-in Codex/Claude aliases, which resolve dependency ranges at launch.

## Deliberately absent

No automatic review retry, answer-file scouting, model vote, custom ACP client,
scheduler, credential provisioning or host-specific sandbox wrapper is included.
These are deliberate scope boundaries, not unfinished features.

The [decision reference](docs/reference.md#deliberately-absent) preserves rejected
mechanisms and the premises behind them. Reconsider one only when those premises
change or an in-scope failure justifies its maintenance cost. Do not restore a
legacy workaround simply because another adapter once needed it.

## Merge

1. Re-read `STATUS.json` and every `RESULT-*.md`, `invalid` ones included, right before merging. A truncated draft is a lead only; never manufacture a peer's final position from it.
2. Check each concrete claim's **premises** against the real tree before its reasoning. A reviewer can fail at the premise: it can reject a correct fix because it misread code it was shown. Absence-of-code, wrong-path and packet-claim findings are packet suspects: open the real file first. A finding is rejected as wrong only on an observation that contradicts its premise or mechanism (a file and line, a run, a live read), never on argument; without one it is uncertain. The orchestrator usually wrote what is under review, so it is the one judge no label can blind, and it favours its own work most where it is unsure; this rule turns that unsureness into an uncertain row instead of a rejection. Out-of-scope findings follow the admission rule.
3. Never tally reviewers. Agreement counts only when premises were derived independently; same-family lanes are correlated. Volume is not quality, and a small delta is no reason to drop the expensive lane.
4. Judge each repair separately from its diagnosis: a correct diagnosis does not make its repair correct. The verification must be able to fail while the defect is present; two checks that read the same state are one signal. When no independent check exists, record the limitation or ask the operator to check; never claim completeness. For stored state, test that omission preserves the last value unless the contract explicitly calls for deletion. When comparing designs, a peer's preference for its own design is not evidence; take only the defects it names in the others. Compare on the requirements the frozen plan stated before launch, and apply a requirement learned from a peer to every design, the plan included.
5. Write `CONSENSUS.md`: one decision line; a confirmed / rejected / uncertain table with lane provenance; for each confirmed finding the selected repair, its verification, and why the host chose it when it differs from the peer's; lanes with no usable answer, never counted as disagreement. Uncertain is a finished ruling, not an undecided one: list every uncertain row for the operator in the next chat message. If no peer produced substantive evidence, say the review is unverified and why; relaunching an unchanged packet is not progress.
6. Verify every applied fix with a regression that fails on the defect. Use a fresh peer round only when an admitted finding or uncertain contract requires independent evidence that local checks cannot provide; scope it to the delta and the contracts it touches, with the decision lineage staged. Do not spend another subscription round merely to ratify completed deterministic repairs. Stop when no admitted finding remains.

**Admission rule.** A finding earns code only if its failure has happened or is likely within scope, and then it gets a tested repair. A real but out-of-scope finding earns one line in Current limits. Anything else is rejected in `CONSENSUS.md` with its reason. Ask reviewers what they would delete, not only whether something is correct. Never add prose to pre-empt a reviewer. Prefer fewer mechanisms and less duplicated state. A necessary repair may add focused code and tests; a new mechanism must justify its maintenance cost against an in-scope failure.

Before consolidating a mechanism, identify its requirement, motivating failure, current owner, regression evidence and retirement condition. Preserve operational nuance even when replacing its implementation. Historical rejected proposals are conditional on their stated premises; refresh them when native capabilities or authentication custody change. The [architecture contracts](docs/architecture.md#requirements-preserved-through-consolidation) provide the lifecycle map.

Fixes go into the reviewed project with a test; protocol lessons go into this note as rules, not stories.

## Repository

Runtime ownership and regression mapping live in [architecture](docs/architecture.md).
The [testing reference](docs/reference.md#repository) covers the suite, fake
harnesses and optional long acceptance test. The runtime needs Python 3.12+ and
native peer CLIs; its Python code is standard library only.

```bash
uv run --python '>=3.12' --with-requirements requirements-test.txt pytest
python3 -m unittest discover -s tools/tests -v
python3 examples/offline_demo.py
git diff --check
```

Tests must use disposable HOME, run roots and fake CLIs, never a live run directory.
CI exercises the publication guard and offline demo without accounts or network;
it does not certify macOS-specific adapters or live providers. After changing
`bin/` or `xcheck/`, reinstall from the intended checkout and run `xcheck doctor`.
Transport or permission changes also need the live qualification above.

Before publishing, follow [the publication policy](docs/publication.md). Private
configuration, deployment details and live run records stay outside tracked source.
History is `git log`; durable review rules live here, not in TODO or ledger files.
