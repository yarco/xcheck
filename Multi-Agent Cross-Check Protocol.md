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
   | Codex | absolute `~/.local/bin/xcheck run … --detach`, then absolute `~/.local/bin/xcheck wait --base B --timeout S` under the call's timeout. The allow rule written by `install` matches only that absolute path followed by `run`, `wait`, `status`, `doctor`, `capacity` or `models`: no `cd … &&`, pipe, redirect, `NAME=value` prefix, or trailing punctuation. It runs the command outside Codex's sandbox with no approval; its target is under `~/.local`, which that sandbox cannot write. Never point it at the vault source |
   | Grok | Herdr mode only (its sandbox covers its whole process tree, so `run` refuses headless). Background `xcheck run`, then one `get_command_or_subagent_output` with `timeout_ms` up to 3 600 000 |
   | opencode | `--detach`, then `wait --timeout S` with the shell call's `timeout` above S. Its shell tool has no background parameter and defaults to 120 s; the detached supervisor is in its own session and survives the call. `--orch opencode` drops opencode lanes |
   | pi | `--detach` + `wait --timeout <call cap>`; never pi's own `delegate`/`subagent` tools as peers (they start pi) |
   | Agy | allowed; `--orch agy` drops every Agy lane |
   | Devin | allowed; `--orch devin` drops Devin lanes |
   | anything else | `--orch other`; one blocking wait or `--detach` + `wait --timeout S`. Never claim a different supported harness to bypass same-harness exclusion |

   `wait --timeout S` exits 3 while lanes still run and prints the current table; each extra return is one short message.
5. **Report** every non-`done` lane in the next chat message from `STATUS.json` (`reason`, `stderr_tail`): `<lane> <status> — <cause in plain words>. Fix: <remedy>. Continuing with <others>.`
6. **Merge** (below) into `BASE/CONSENSUS.md`.

`run` options: `--lanes kind:model[:effort],…` replaces the roster (Agy and Devin take no effort, opencode's is an optional `--variant`, which exists only on `opencode run`, so the app view refuses it before launch; every other kind needs one); `--add LANE` appends an opt-in lane to it. `--mode auto|headless|herdr` (auto = Herdr when available). `--view stream|tui` defaults from placement (stream headless, tui in Herdr); it exists so tests and controlled smokes can drive the app path without Herdr. `--web live` (default: peers may search and must cite what they relied on) or `packet` (built-in web tools off and peers told not to fetch; connected MCP tools remain, so this is not isolation). `--idle` overrides the view's inactivity limit (180 s for apps, 900 s for streams). `--cap` is each lane's spend budget (3600 s); a lane queued behind another run waits up to `--cap` in its own process, then ends `skipped`. `--force-lane NAME` launches a selected lane past a capacity refusal, only on the operator's say-so and announced in chat. `--skip-capacity` is for controlled smokes. `--detach` returns once launched.

Invalid options (a BASE that does not resolve under `/tmp/agent-offload`, no peers, non-positive budgets, a forced lane not in the roster, a same-kind peer) are refused before any pane, model or launch marker exists. Inside that root any slug works (spaces, quotes, non-ASCII, nesting, a relative path or a symlink that resolves there), and peers always run in the fixed `.lanes/<lane>` workspaces, so no BASE spelling reaches a trust or approval prompt. Other commands: `xcheck wait --base B [--timeout S]` (blocks; also takes over a dead supervisor and finishes its job), `xcheck status --base B` (render from files).

Exit codes: 0 every lane settled (read the table); 2 refused before any spend; 3 `wait` timed out while lanes run; 130 supervisor interrupted.

## Roster

| Lane | Pool |
|---|---|
| `codex-gpt-6.1-sol-high` | codex |
| `claude-opus-5-5-high` | claude |
| `grok-4.7-xhigh` | grok |
| `agy-gemini-3.1-pro-high` | agy-gemini |
| `agy-gemini-3.8-flash-high` | agy-gemini |
| `agy-<third-party-model>` | agy-3p |

The orchestrator's harness is removed automatically. A lane name is its provenance (`<harness>-<exact model>[-<effort>]`) and names exactly what is launched. Pins live in `xcheck/adapters.py`; `doctor` confirms the Grok, Agy and Devin pins appear in `grok models` / `agy models` / `devin models list`. OpenCode offers are discovered when requested, not pinned. Refresh pins deliberately after checking the installed CLIs: when `agy models` lists a newer Flash, replace that one row and leave Pro and Sonnet alone. Claude pins use the versioned id (`claude-opus-5-5`, not `opus-5-5`); capacity maps it to the opus bucket. Effort tiers are per model: an older Grok given `xhigh` fails at launch rather than downgrading. An Agy selector carries its own effort; check a selector with the zero-token `agy --model <selector> -p /model` and `-p /effort` (a fixed model reports `not adjustable`).

Opt-in only (`--add`; `adapters.OPT_IN`, pins checked by `doctor`): `agy:<third-party-selector>` (expensive, and correlated with the Sonnet lane), and `devin:swe-2-max` (the operator's current Devin default). OpenCode extras are discovered dynamically with `xcheck models`, rather than maintained as a model pin. Use `--add opencode:<provider>/<model>[:variant]`; an OpenRouter `:free` suffix is part of the model, with an optional variant after it. Slashes and colons become hyphens in lane names, and the exact model ID reaches OpenCode unchanged. Check the selected provider's data policy before sharing a packet; Contributor and some trial offers permit training or logging. Never: `agy:gpt-oss-120b-medium`; `agy models` lists it, but its agent loop aborts with no answer.

`xcheck models` reads provider-owned sources on every invocation. [OpenRouter's live model API](https://openrouter.ai/api/v1/models) reports prices, modalities and tool support. Zen's [live model list](https://opencode.ai/zen/v1/models) reports IDs only, so those IDs are joined to the endpoint and pricing tables on its [production documentation](https://opencode.ai/docs/zen/). A documented free model absent from the live list is omitted; System One decision models are excluded. Cerebras' [public pricing API](https://inference-docs.cerebras.ai/api-reference/models/public-models) is checked too; its current offers are paid and its expiring trial credits are not treated as free models. Every reported price component must be known and zero. A failed source omits that provider and reports the gap; there is no stale pricing fallback.

```bash
xcheck models                              # current provider-confirmed zero-price offers
xcheck models --provider openrouter --json # sources, exact selectors, client support and account variables
xcheck models --provider opencode          # Zen's live availability + current documented pricing
xcheck models --sources /path/sources.json # extend provider discovery without maintaining model IDs
```

`--provider` is repeatable and selects a discovery source; unknown sources are refused. `--limit` defaults to 20; undated offers appear first so newly published models are not hidden by lagging dates, then dated offers sort newest first. OpenCode's refreshed verbose listing supplies local client support and, for Zen, tool capabilities and advisory release dates. Its Models.dev prices and deprecation markers do not establish provider availability or price. New provider models missing from the client listing remain visible as `needs-client-setup`; only locally supported tool models get a printed `--add` selector. A locally deprecated model is also marked for client setup. OpenRouter creation dates come from its API; Zen's API generates a timestamp for every request, so those timestamps are never treated as model release dates. Sources and date provenance are included in JSON. Explicit `opencode/<model>` and unprefixed Zen lanes share one quota pool.

A sources file maps provider IDs to definitions and adds to or overrides the built-ins. Example: `{"vendor":{"format":"priced-json","models_url":"https://vendor.example/models","docs_url":"https://vendor.example/docs","auth_env":["VENDOR_API_KEY"]}}`. URLs must be public HTTPS without credentials; `auth_env` holds informational setup names only and does not change lane credentials; connect added providers through OpenCode’s stored auth or provider configuration. The lane environment retains only the existing explicitly permitted provider keys, not arbitrary keys declared by a discovery source. `priced-json` accepts a `data` array with `id`, `pricing.prompt/completion` and any other charges, text input/output modalities (under `architecture` or at top level), and `tools` under `supported_parameters` or `supported_features`. Optional `created` is an epoch release date, `expiration_date` an ISO date, and `deprecated`/router metadata exclude those models. The `zen` format uses the documented endpoint/pricing HTML tables. Other response formats need a focused adapter; unverified providers are never offered based on a third-party zero price.

Copy a ready `--add` selector into a new review run after connecting the provider and checking its account quota and data policy. Zero token prices do not establish account readiness or review reliability; some trial or Contributor offers permit logging or training. Discovery performs no inference, config edit, roster change, or automatic substitution. Sources checked 2026-10-01; no local model snapshot is stored deliberately, because the offers change.

Gemini Pro and Flash are different evidence: Flash is the ratifying lane (it tends to agree with the packet's claims, false ones included) and Pro is the one that disagrees, so keep Pro even when Flash also ends `done`. `done` certifies the output contract, not review quality. Same-family lanes (two Claude-family lanes, or `agy-<third-party-model>` under a Claude orchestrator) are correlated. Returns diminish after about three strong, independent peers; add a lane for a distinct capability or failure hypothesis, not for volume. The roster is fixed at launch; add a reviewer in a fresh BASE.

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

The next line is packet metadata; do not include it in your answer.
RESULT_REQUIRE: VERDICT
Final line, alone: RESULT_COMPLETE
If you cannot do the review, make the final line RESULT_INVALID instead.
```

The markers are literal lines: no period, code fence, quote or explanation after `RESULT_COMPLETE` / `RESULT_INVALID`. PROOF is `6) PROOF: <token>`, or a bare `6) PROOF` heading followed by the token alone or by a `PROOF: <token>` line (bold, bullets or backticks around either are fine); anything else after `PROOF:` is compared as the token and fails, and so does pasting the whole proof file. An answer also needs review text beyond the heading, PROOF and marker. `tests/test_laws.py` states the whole grammar as properties.

**What every lane is told** (`run.MESSAGE`): the owner convened the review; read `PACKET.md` then `PROMPT.md` (PROMPT decides format); work from the staged directory; connected MCP tools are available, external actions (sending messages, changing records) are not; nobody answers questions mid-review. Codex, Claude and Agy use tools that read (plus web in live mode); Grok and opencode may also run Bash and write disposable scratch inside their own workspace, never the staged files. Stream lanes print the answer as their final message; app lanes write only `ANSWER.md`, in one complete write.

**What `run` adds to BASE**: `run.json` (roster, contract, settings, Herdr pane ids; written once), `lanes/<lane>/` (`message.txt`, `staged` stamp, `pids.json`, `exit.json`, `stream.jsonl` + `view.log` or `screen.log`, `stderr.log`, `harness.log` where the CLI supports one), `RESULT-<lane>.md` (the captured answer), `STATUS.json` / `STATUS.md` (one row per selected lane, skipped ones included). Peers work in stable workspaces `/tmp/agent-offload/.lanes/<lane>/`, restaged only after pool admission and reused by the next run of that lane; inspect a run's evidence in its BASE, not in a later run's workspace. Artifacts stay in `/tmp`; never copy run directories into the vault.

## Lane statuses

| Status | Meaning | Orchestrator action |
|---|---|---|
| `done` | answer ends `RESULT_COMPLETE`, has the required heading and review text, PROOF matches | read and adjudicate |
| `invalid` | an answer exists but breaks the contract (reason names how), or the peer declared `RESULT_INVALID` | read it anyway; its findings are leads; tell the operator |
| `failed` | no answer; reason is the harness's structured error, the app's limit banner, else its first stderr line | read `reason` + `stderr_tail`; tell the operator cause and remedy |
| `timeout` | idle limit, `--cap`, or the runner's own bound ended it | tell the operator; partial output is in the stream or `screen.log`, the last screen in `exit.json` |
| `skipped` | never spent: capacity reserve, auth, unknown capacity, or pool busy past `--cap` | tell the operator; `--force-lane` only on their say-so |
| `interrupted` | the runner or headless supervisor was signalled | reattach with `wait`, or rerun in a fresh BASE |

Never relaunch a lane that died of quota or auth in the same run. A lane lost to a bad packet or prompt is rerun in a fresh BASE after fixing the packet, and the operator is told. A lane that dies within seconds with a usage message usually means a flag drifted: read the first stderr line, run `xcheck doctor`, and compare the installed CLI's `--help` before blaming quota or the model. The only answer channels are a stream's final message and `ANSWER.md`; a review written under another name (Legacy saw Agy use `RESULT.md`) ends `failed` and is not scouted. Read its saved screen or stream, and the file in `/tmp/agent-offload/.lanes/<lane>/`, before the next run of that lane restages it.

## Placement and views

**Headless**: each lane runner is a child of the supervisor in its own session; its rendered output goes to `view.log`. A signal to the supervisor stops the lanes (`interrupted`).

**Herdr**: chosen when xcheck can name the caller's pane, `herdr pane layout` answers, and the tab is at least 176×14. The pane is `HERDR_PANE_ID` when `HERDR_ENV=1`. A Codex app attached to Codex's shared app-server daemon runs its tool shells as daemon children outside the pane, so they carry no `HERDR_*`; its pane is then the one whose title (`herdr pane list`) holds a ≥20-character prefix of the shell's `CODEX_THREAD_ID`. That needs `thread-id` in `[tui] terminal_title` of `~/.codex/config.toml` (set: `["spinner", "thread", "project", "thread-id"]`; Codex elides the id to 29 characters) and a Codex app started after it was set. Zero or several matching panes mean headless. Re-verify: plain `codex` in a Herdr pane and in a terminal outside Herdr, each running `python3 -c 'from xcheck import herdr; print(herdr.caller())'` from this folder: the pane id, then `None`. The first lane splits right of the orchestrator (`--ratio 0.62`: the orchestrator keeps 62%); the rest split that column into equal rows in roster order, titled `xcheck:<lane>`. Lane panes are addressed only by the ids their split returned, never by title or focus, and never by `--current`. Each pane runs `exec <lane runner>`, so the pane is the runner: Herdr closes it when the runner exits, three seconds after its final line. Nothing closes panes by id. If the supervisor dies, panes finish on their own and `wait` reattaches.

**App view** (`tui`, default in Herdr; Agy always): the runner hosts the harness's real interactive app in a pty it owns, with the initial prompt as an argument, so nothing is typed into the app and there is no readiness to detect. App output reaches the pane unchanged; the operator's keystrokes reach the app. The answer is published once `ANSWER.md`'s last line is `RESULT_COMPLETE` or `RESULT_INVALID` and the file has not changed for 3 s; then the runner ends the app's whole process group. An app that draws nothing for its idle limit (a static dialog, a quota modal) times out with its last screen in `exit.json`; no prompt is resent. Only drawn text resets the idle clock: control sequences alone do not, because a blocked Devin dialog emits empty synchronized-update frames forever. A repaint after a resize is drawn text, so a pane that grows when a sibling closes restarts its lane's clock. An app that has drawn nothing for 15 s with a limit banner (`adapters.APP_LIMIT`: Agy `⚠ Individual quota reached`, Claude `You've hit your limit`, Codex `You hit your weekly limit`, each copied from a real screen) at a line start in its last 10 lines ends `failed` with the banner and its reset time. A peer that quotes a banner keeps drawing and is never ended by it. Add a pattern only from a captured screen, with its fixture.

**Stream view** (headless default): the harness's non-interactive mode; the runner reads its structured event stream, and the answer is its final message. Only stdout or a stable `ANSWER.md` resets the idle clock; stderr does not.

Completion is always the lane's `exit.json`, never pane text.

## Capacity

Each pool has one free meter that runs no inference, read by `xcheck/capacity.py`: Codex `app-server` `account/rateLimits/read` (5 h and weekly windows), Claude's OAuth usage endpoint with the keychain token (its `limits[]` list: session, weekly, and a weekly cap scoped to the lane's model family, where a scoped cap it cannot attribute counts; the flat `five_hour`/`seven_day` keys only when `limits[]` is absent), Grok's billing credits view (the worse of the account and Grok Build meters; the monthly view reports no allowance on this account), and Agy's `agy -p /usage` (zero turns; the lane family's 5 h and weekly buckets). `xcheck capacity [--lanes …]` prints the readings with the same reserves `run` would apply. Each lane reads its own pool after admission and immediately before spending, so the reading is not stale and, in Herdr, runs outside any orchestrator sandbox. A lane spends only on `eligible`: the tightest window must have more than the reserve left, 10%, or `5 + 5n`% for the run's `n` lanes in one Agy family (capped at 90). The reserve protects the operator's interactive quota, which a runtime quota error arrives too late to protect. A meter that cannot be read is `capacity-unknown` and a rejected login is `auth-required`; with `reserve-low` they end the lane `skipped`. There is no fallback meter.

- Agy's `/usage` is served by its persistent headless `language_server`; the launchd agent `a launchd sidecar agent` keeps one running.
- Claude `security` exit 36 is a locked login keychain (a remote session): run `keychain-unlock-gui` with the operator at the screen. Never `security unlock-keychain` from an agent shell: control returns before a password can be typed. `security show-keychain-info ~/Library/Keychains/login.keychain-db` checks the state without prompting. Otherwise pass a fresh reading from Claude Code's `/usage`, the lowest window shown, as `XCHECK_CLAUDE_REMAINING=<percent left>` and `XCHECK_CLAUDE_REMAINING_AT=<ISO time>` (at most 5 min old) in the environment the runner inherits, i.e. headless placement; it overrides the keychain, so unset both right after that one launch. Exit 44 means no login.
- The Claude meter checks the keychain token's millisecond `expiresAt` before HTTP, including a 60 s margin for the probe and clock skew. An expired or near-expiry token returns `auth-required`: open Claude with `/status` to let its native auth path refresh it, exit, then recheck capacity; if it cannot refresh, run `env -u ANTHROPIC_API_KEY claude auth login --claudeai`. `claude auth status` can still say logged in while the token is expired, and Anthropic can return 429 for that expired token. Malformed present expiry fails closed; missing expiry preserves the existing read path. No custom token refresh or fallback meter is used.
- The Claude usage endpoint also returns HTTP 429 when polled too often (CodexBar reads it too). The meter accepts positive finite `Retry-After` seconds or an HTTP date, otherwise 2, 4, 8, 16 s; zero/negative/nonfinite delays cannot spin. Requests and sleeps share a 30 s probe budget, with at most five requests, and then fail closed as `capacity-unknown`. A long throttle reports its delay and recheck remedy. Recheck with `xcheck capacity --lanes claude:claude-opus-5-5:high`; once it reads `eligible`, add the lane in a fresh BASE. `--force-lane` does not apply here: a launched BASE refuses a second `run`.
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

`xcheck doctor` checks that every flag the adapters use appears in the installed CLI's `--help`, and the Grok/Agy model pins. Tests pin argv construction and captured stream shapes; neither proves a live tool call. After a CLI upgrade, re-verify a changed adapter with a small complete packet on a capacity-eligible lane, in both placements (`--mode headless` and Herdr). The packet must require the peer to run one shell command, fetch one URL and call one read-only MCP tool, and report each result: a lane that happens to use no tool proves nothing about its permission flags. Self-update is off in every lane: Codex `check_for_update_on_startup=false`, `DISABLE_AUTOUPDATER=1` for Claude, `GROK_DISABLE_AUTOUPDATER=1`.

**Codex.** Stream: `codex exec` with `-s read-only`, the message on stdin, `--json`, and `-o` for the clean final message. Web is the `web_search` config key (`live|disabled`); `exec` has no `--search`, while the app's switch is `--search`. User config loads, so connected MCP servers are available; never `--ignore-user-config`. The read-only sandbox still lets it run read commands anywhere. Errors arrive as a top-level `error` event and `turn.failed.error.message`. The app runs `-s workspace-write -a never` so it can write `ANSWER.md`, with `history.persistence="none"`. It shows a trust screen for any directory not trusted by exact path (a trusted parent does not count; `-c projects…trust_level` on the command line does not suppress it), so `xcheck/trust.py` records each stable workspace under both `/tmp` and `/private/tmp` spellings in `~/.codex/config.toml`. It edits only the canonical `[projects."<dir>"]` + `trust_level = "…"` layout and refuses anything else; a TOML round-trip must show only those trust levels changed. On refusal, trust the workspace by hand; never auto-click a dialog or normalise the file.

**Claude Code.** `--restricted` ignores settings files and hooks, confines the file tools to the working directories (a read of `/etc/hosts` is refused), and, despite its help text, does not load the operator's MCP servers from `~/.claude.json`; account connectors (claude.ai Gmail, Drive, …) load either way. So `--mcp-config ~/.claude.json` loads them, once. `--tools` lists what exists (Read, plus WebSearch/WebFetch in live mode, plus Write in the app); `--allowedTools` pre-approves that list plus `mcp__<server>` for every server in `~/.claude.json` and every `claudeAiMcpEverConnected` connector (`claude.ai Gmail` → `mcp__claude_ai_Gmail`). `mcp__*` is not honoured as a pattern, and `bypassPermissions` is refused in restricted mode, so each server is named. A server added after launch is denied in that run. Keep both lists: without `--allowedTools` a tool would prompt. Stream mode adds `--permission-prompts none` (anything that would prompt is denied instead of hanging) and `--include-partial-messages` (keeps the stream moving through a long think, so the idle clock measures real silence). Never `--bare`: it skips the keychain, and subscription auth fails. The final text is the `result` event; with `is_error: true` that field is the error message even when `subtype` says `success`, so it is never an answer. `rate_limit_event` carries five-hour and seven-day utilisation.

**Grok.** `--sandbox read-only` reads everywhere and kernel-denies writes outside `~/.grok` and tmp. `strict` cannot read `/opt/homebrew`, so the operator's global hooks that exec a binary fail with exit 126 and Grok ignores the failure; `off` removes write protection. The operator's shell exports `GROK_SANDBOX`, which would otherwise choose the profile, so the lane environment drops it. Re-verify with `--debug-file` and grep `hook failed` / `exit code 126`. `--always-approve` lets Bash and connected MCP tools run without a prompt in both views. `--tools` is honoured only headless; the app ignores it. Memory is off through `GROK_MEMORY=0` (`--no-memory` is accepted but absent from `--help`). The stream answer is the last model message after the last `tool_call`; a `usage` event closes each message, so earlier narration is never glued onto the answer. Errors are `{"type":"error","message":…}`. Docs: `~/.grok/docs/user-guide/` (10-hooks, 14-headless-mode, 18-sandbox, 20-background-tasks).

**Agy** always runs as its app, headless placement included: print mode soft-denies a tool confirmation after a sandbox-blocked command and ends the whole turn with no answer. The app runs `--sandbox --dangerously-skip-permissions --mode accept-edits`, so nothing asks. Never pass `--effort` (the selector carries it; fixed selectors reject the flag), and never `--disable-slash-commands` (it also disables plan mode). Its file tools reached every calibration fragment of a 952 KB, seven-file packet on both Flash and Pro, so no inline bundle or size cap applies; that proves reach, not exhaustive reading. Agy starts its own configured MCP servers, exposes a generic `call_mcp_tool`, and has no tool allowlist, so its external-action and packet-mode web limits are instruction-only. `xcheck/trust.py` adds each workspace to `trustedWorkspaces` and sets `showFeedbackSurvey=false` in `~/.gemini/antigravity-cli/settings.json`.

**opencode** (1.18.33). A model without a `/` is launched as `opencode/<model>` (Zen). A model with a `/` is `provider/model` as written; those lanes keep that provider's env keys (`ZAI_API_KEY` and `ZHIPU_API_KEY` for Z.ai, `OPENROUTER_API_KEY` for OpenRouter) because stored login is per-provider. Without its key, `opencode run` on Z.ai fails with only `Unexpected server error`, and a launchd session exports `ZAI_API_KEY` empty, so a Z.ai lane whose key is empty ends `skipped: auth-required` naming the variable, before admission. Zen's free tier serves only opencode's stock tool set: any permission rule, even a lone `bash: deny` in `OPENCODE_CONFIG_CONTENT` or a custom agent, is refused with 403 `FreeTierError: free tier can only be used from within OpenCode`. So the tools stay stock and the lane environment drops every `OPENCODE_*` variable. `--auto` answers permission asks, because an unanswered ask in `opencode run` waits silently forever. A Seatbelt deny-write wrap was tried and dropped: `sandbox_apply` on that profile fails in the lane process (exit 71) and the lane never starts. The write boundary is the review instruction plus OpenCode's cwd. Bash and the edit tools get `Operation not permitted` anywhere else, and MCP servers that must write elsewhere (uv-launched ones such as semble) fail to start. Startup can take a minute while MCP servers load; that is silence inside the stream idle limit. Stream: `opencode run --format json` emits each text part once, complete (a 14-line answer is one `text` event), not as deltas; the answer is the text parts after the last `tool_use`, and errors arrive as `{"type":"error","error":{"data":{"message":…}}}` (a free-usage limit is `FreeUsageLimitError`, HTTP 429, with no published quota). App: `opencode <work> -m … --prompt …`. Web and packet-mode limits are instruction-only here, as for MCP. Provider-prefixed lanes of one run take separate quota pools (`opencode-<provider>`), so they can spend in parallel.

**Devin** (3000.11.3). Always its app, like Agy: `-p` prints plain text and the stream reader only parses JSON, so the answer is `ANSWER.md`. `devin --config <overlay> --permission-mode accept-edits --respect-workspace-trust false --model … -- <prompt>`. The overlay is the operator's config with `hooks` emptied (moshi/Herdr PermissionRequest would hang; I3). `accept-edits` auto-approves only workspace edits; every shell command, web fetch and MCP call still prompts, and a prompt hangs the lane. So the overlay appends `exec` (the whole shell tool), `mcp__*`, and in live mode `Fetch(https://*/*)` / `Fetch(http://*/*)` to the operator's `permissions.allow`; packet mode puts the two fetch rules in `deny`, which refuses without prompting. Edits outside the workspace still prompt. `dangerous` would approve those too, and `smart` asks a model, so neither is deterministic containment. `--config` replaces the whole file, so login, `org_id` and `shell.setup_complete` stay; a stripped overlay made the app ask to use local credentials. `--respect-workspace-trust false` because the app cannot show the trust prompt. Never `--sandbox` or `DEVIN_SANDBOX`: that isolation hid `credentials.toml` and the app asked to use local credentials. Never `--cloud`. The operator shell exports `DEVIN_PERMISSION_MODE`; the lane sets `accept-edits`. Web and MCP limits are instruction-only (it imports Claude's user servers). Pin: `devin models list`. The pin is optional and can be removed from a roster without changing the protocol.

**Herdr** (0.9.3). `pane list` gives each pane's `pane_id` and `terminal_title_stripped`; `pane layout --pane <id>` gives tab size; `pane split --pane <id> --direction right|down --ratio <share kept> --cwd <dir> --no-focus` returns `.result.pane.pane_id`; `pane run <id> <command>` types into the pane's shell (typeahead is buffered, so no readiness wait is needed); a pane closes when its process exits.

**Lane environment.** The orchestrator exports its session identity into every child shell (`CLAUDECODE`, `CLAUDE_CODE_*`, `CLAUDE_PID`, `CLAUDE_EFFORT`, `CLAUDE_PLUGIN_*`, `CODEX_COMPANION_*`, `CODEX_SANDBOX*`, `OPENCODE*`, `HERDR_*`). A peer that inherits them becomes a nested session of the orchestrator, runs at its effort, or can drive the operator's Herdr. Removing `HERDR_*` alone does not stop that: the `herdr` CLI falls back to its default socket, so the runner sets `HERDR_SOCKET_PATH` to a path that cannot exist and every `herdr` call in a lane finds no server. Codex and Devin run each shell command through the operator's zsh rc files, and `~/.zshrc` loads every API key from the Keychain (`the operator's keychain secret loader`), so the runner also sets `the lane guard variable` and `~/.zshrc` (the managed rc source) skips that load when it is set. Keep that guard on any new secret loader. Re-verify with a lane that runs `env | grep -c _API_KEY`: it must print 0 in every harness. The runner removes them, `GROK_SANDBOX`, `OLDPWD`, and every `*_API_KEY` except the provider key an opencode `provider/model` lane needs (`ZAI_API_KEY`, `OPENROUTER_API_KEY`), and sets `PWD` to the lane workspace: opencode roots its file tools and shell at `PWD`, not at its real working directory, so an inherited `PWD` points it at the orchestrator's directory. Harmless preferences such as `GROK_TELEMETRY_*` pass through. Re-verify: `env` in the orchestrator's shell, then the fake-harness env test.

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

- Distinct model IDs can produce the same sanitised lane name; a roster with duplicate names is refused before launch. The exact ID remains in run metadata and argv.
- Connected MCP tools run without per-call review. The ban on external actions and packet-mode web limits is an instruction, not a tool boundary; `--web packet` is not a security boundary for untrusted material.
- Grok can write anywhere in tmp, opencode anywhere in `/private/var/folders`, and Devin's shell anywhere the operator can (its sandbox hides the login, so the lane runs without one), including another lane's scratch; their own-workspace limit is an instruction. Codex (read-only sandbox) and Claude (no shell) cannot.
- Grok's and opencode's OS sandboxes stop MCP servers that must write elsewhere (uv-launched semble fails its handshake), so those lanes see fewer MCP tools. The same sandbox keeps the Grok app from saving `-m` as the operator's default model ("Couldn't save preferred model: Operation not permitted" is expected).
- A static account-verification splash can hit the 180 s app idle; the last screen is kept and the lane ends `timeout`.
- A Herdr failure after a split but before `pane run` leaves a pane with an idle shell; close it by hand. (A failed `exec` makes zsh exit, so that pane closes itself.) Either way the lane is settled `failed: lane runner never started` after 90 s.
- `--cap` ends a lane; there is no late harvest. Rerun in a fresh BASE.
- Blinding covers labels only. Style, the reasoning itself, and connected tools (pond session history, qmd over this vault) can still tell a peer who wrote something or how the host ruled.
- If a runner died and `ps` is unavailable, `wait` cannot confirm the orphan's identity and leaves it alone, noting that in the reason; its pool stays held until the orphan exits. Never kill a recorded PID on a guess.
- A full roster starts many MCP servers in the same second, which can exceed Keenable's 10 requests/s limit (HTTP 429); the lane that loses the race lacks that server for its session and keeps its built-in web tools.
- Claude's app view sends its first turn before MCP servers finish connecting; a slow server (qmd under load took 25 s) can be missing from a short review. The stream view waits for connections.
- Codex's quota error is captured (`tests/fixtures/codex-quota.jsonl`), and so is Agy's per-model quota screen (`tests/fixtures/agy-quota.screen`). Claude, Grok, opencode and Devin quota errors, and every auth error stream, are not; their invalid-model fixtures prove parser shape only. Keep the next real one as a fixture.

## Deliberately absent

Legacy (archived read-only in `../x-check-legacy/`, never a launcher) solved problems this design does not have. Do not re-add these without a failure that happened within scope:

| Legacy mechanism | Why it is not needed |
|---|---|
| Credit/auth log-regex classifiers over whole logs | their only consumer was the retry decision, and prose that quoted a token killed healthy peers; structured errors name the cause, nothing retries, and the one screen check (`adapters.APP_LIMIT`) is anchored, bounded to the last screen and gated on the app having stopped drawing |
| Late harvest, file scouting, provisional labels, detached cleanup worker, notices file | lanes are owned processes that end with `exit.json`; one declared answer channel; the orchestrator reports from `STATUS.json` |
| Spender claims, PGID fallback, boot identity, daemon reaper, global lock with live-lane scans | every spender is our runner or its child; kernel lock lifetime equals spender lifetime; per-pool admission keeps disjoint pools parallel |
| Supervisor watchdog, pane-close and pane-ownership bookkeeping | the runner's alarm bounds it; `exec` makes each pane close with its runner |
| Herdr `agent start` readiness, splash classifiers, prompt resend | the prompt is argv in a pty; a static screen becomes a bounded timeout |
| Agy stdin bundle, 192 KB cap, read-depth probe | file tools reach large packets; the probe was gameable by grep |
| Host router with note read-map, wait metrics, `LIVE_CHECK_CAP` gate | `--mode auto` places; the note fits one read; token discipline is structural; the cap was a prompt contract, not enforcement |
| Perplexity lane | not a default peer; its failure shapes stay documented in Legacy |
| An opencode reviewer agent or permission rules | the free tier refuses anything but opencode's stock tool set; the Seatbelt profile is the boundary |
| `devin acp --agent-type review` | ACP, which the runner does not speak; the app writes `ANSWER.md` like Agy |
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
| Check the served model against the lane pin | only Claude's stream names the model that answered; Codex and opencode streams and the apps do not. Every lane passes an explicit model and no fallback flag (Claude's `--fallback-model` is never passed) |
| Hide lane names from the orchestrator during the merge | lane identity is evidence the merge uses (Flash ratifies, same-family lanes correlate), style names the lane anyway, and the orchestrator's bias runs toward its own work, which no label hides |
| Paraphrase or style-normalise answers to hide authorship | light perturbation helps, but full normalisation lets agreement with the judge's own views take over as the preference (arXiv 2512.05379), and a paraphrase can change what a finding claims |
| A self-preference warning in `PROMPT.md` or the merge | a one-line bias warning did not move self- or perceived-authorship effects when measured |
| Let peers rank each other's answers (council stage 2) | a judge favours its own entry even when anonymised, and a ranking is a preference, not a falsifiable finding |

## Merge

1. Re-read `STATUS.json` and every `RESULT-*.md`, `invalid` ones included, right before merging. A truncated draft is a lead only; never manufacture a peer's final position from it.
2. Check each concrete claim's **premises** against the real tree before its reasoning. The ratifying lane fails at the premise: it can reject a correct fix because it misread code it was shown. Absence-of-code, wrong-path and packet-claim findings are packet suspects: open the real file first. A finding is rejected as wrong only on an observation that contradicts its premise or mechanism (a file and line, a run, a live read), never on argument; without one it is uncertain. The orchestrator usually wrote what is under review, so it is the one judge no label can blind, and it favours its own work most where it is unsure; this rule turns that unsureness into an uncertain row instead of a rejection. Out-of-scope findings follow the admission rule.
3. Never tally reviewers. Agreement counts only when premises were derived independently; same-family lanes are correlated. Volume is not quality, and a small delta is no reason to drop the expensive lane.
4. Judge each repair separately from its diagnosis: a correct diagnosis does not make its repair correct. The verification must be able to fail while the defect is present; two checks that read the same state are one signal. When no independent check exists, record the limitation or ask the operator to check; never claim completeness. For stored state, test that omission preserves the last value unless the contract explicitly calls for deletion. When comparing designs, a peer's preference for its own design is not evidence; take only the defects it names in the others. Compare on the requirements the frozen plan stated before launch, and apply a requirement learned from a peer to every design, the plan included.
5. Write `CONSENSUS.md`: one decision line; a confirmed / rejected / uncertain table with lane provenance; for each confirmed finding the selected repair, its verification, and why the host chose it when it differs from the peer's; lanes with no usable answer, never counted as disagreement. Uncertain is a finished ruling, not an undecided one: list every uncertain row for the operator in the next chat message. If no peer produced substantive evidence, say the review is unverified and why; relaunching an unchanged packet is not progress.
6. Review applied fixes in a fresh round scoped to the delta and the contracts it touches, with the decision lineage staged. Stop when no admitted finding remains.

**Admission rule.** A finding earns code only if its failure has happened or is likely within scope, and then it gets a tested repair. A real but out-of-scope finding earns one line in Current limits. Anything else is rejected in `CONSENSUS.md` with its reason. Ask reviewers what they would delete, not only whether something is correct. Never add prose to pre-empt a reviewer. Prefer fewer mechanisms and less duplicated state. A necessary repair may add focused code and tests; a new mechanism must justify its maintenance cost against an in-scope failure.

Fixes go into the reviewed project with a test; protocol lessons go into this note as rules, not stories.

## Repository

`uv run --with 'hypothesis>=6.100' python3 -m unittest discover -s tests` (about two minutes; run it in the background) and `git diff --check`. Runtime is Python 3.12+ stdlib plus the harness CLIs; there is no venv or lockfile — the runner command supplies the one test dependency, Hypothesis (pinned in `requirements-test.txt`), imported unguarded so a missing install fails by name. `tests/test_xcheck.py` runs end to end against `tests/fake/harness`, which speaks each CLI's stream and app shape, plus captured streams and meter payloads in `tests/fixtures/`; `tests/test_laws.py` holds property laws for the answer grammar, the Grok reader and trust updates. Tests set `XCHECK_ROOT`, `CODEX_CONFIG` and `AGY_SETTINGS` to disposable paths, and the fake `codex`/`agy` also answer the capacity meters (`FAKE_CAPACITY`); never point them at the real `/tmp/agent-offload` (they would queue behind live runs) or the real configs. Dropbox resets new files to mode 600; restore `755` on `bin/xcheck` and `tests/fake/harness`.

After changing `bin/` or `xcheck/`, run `bin/xcheck install`, then `xcheck doctor`. History is `git log`; durable requirements, invariants and limits live here, not in TODO or ledger files.
