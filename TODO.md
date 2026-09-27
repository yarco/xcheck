# TODO — X-Check v2 handoff

Read this whole file before touching anything. Then read
`Multi-Agent Cross-Check Protocol.md` (the protocol note, ~440 lines) and
`git log` in this folder (every commit body explains a decision).

## What this is

The operator's multi-agent cross-check protocol, rewritten from scratch (v2).
v1 (~30.7k lines: two notes, zsh launcher, 4.5k-line Herdr TUI driver, quota
claim system, log-regex classifiers) is archived read-only in the sibling
folder `../x-check-old/` (own git repo; do not edit it). v2 took this folder's
original name because vault links and tools depend on it.

v2 in one paragraph: `xcheck run` fans a staged packet (BASE under
`/tmp/agent-offload/<slug>/`: PACKET.md, PROMPT.md, evidence/) out to peer
agent CLIs (Codex, Claude Code, Grok, Agy; never the orchestrator's own
harness). One lane runner (`xcheck/lane.py`) runs each lane; it is placed as a
headless child or in a Herdr pane. Two views: `stream` (headless default:
harness non-interactive mode, structured NDJSON stream, answer = final message)
and `tui` (Herdr default: the harness's real interactive app hosted in a pty the
runner owns, answer = `ANSWER.md` published once complete and stable). Agy
ALWAYS runs as its app (see below). Per-quota-pool kernel `flock`s
(`xcheck/pools.py`) serialize runs per pool; liveness is a held lock on an
`alive` file (no PIDs/ps). `bin/xcheck install` publishes a read-only copy as
`~/.local/bin/xcheck` (+ Codex allow rule). Tests: `python3 -m unittest
discover -s tests` (~100 s; Hypothesis is the only test dependency).

## Operator requirements (all binding; from the design conversation)

1. Simplify v1 radically but keep every real invariant, precondition, nuance.
   Deterministic code for mechanics; prose for knowledge; the orchestrating
   LLM for judgment. No unneeded helpers: every helper must be justified from
   first principles. Do not reinvent what v1 already solved: check
   `../x-check-old/` notes and code before designing anything.
2. Two modes: Herdr (visible panes) and headless. In Herdr the operator must
   see each harness's REAL interactive app (TUI), live.
3. Never any manual approval/trust click. It must just work.
4. The orchestrator must not spend tokens while lanes work (one blocking call,
   or `--detach` + `wait --timeout S`, exit 3 = still running).
5. Peers get web search by default (`--web packet` only for untrusted input).
6. Codex as orchestrator must use v2 (never route to v1). No runtime in
   `~/.agents` (v2 uses `~/.local/share/xcheck` + `~/.local/bin/xcheck`).
7. Re-check every harness and flag from first principles (installed `--help`,
   vendor docs, a real smoke). Never drop a v1 safeguard on thin evidence:
   test the failure mode it protected against, for every model/lane it covered.
8. Property-law tests (Hypothesis, grammar-built inputs, checked from both
   sides) where the logic is pure.
9. Cross-check the implementation with multiple agents, giving them decision
   lineage and a v1→v2 gap ledger so they do not re-report deliberate choices.
10. Final goal: the most elegant long-term design, simpler but not basic,
    nuanced where it matters, researched (online too) for inspiration.

## Current state (2026-09-27 continuation)

P0-1 through P0-8 are complete. The installed copy was refreshed after the
adapter changes, and `xcheck doctor` passes. The full suite passes 66 tests.
The operator approved removal of nine old per-run Codex trust entries and 44
old per-run Agy entries; stable `/.lanes/` entries were retained.

The operator subsequently requested Bash for Grok in both views and all
connected MCP tools for every peer. Grok now has Bash under its `read-only`
OS sandbox and may write disposable scratch files in its own workspace. All
connected MCP tools are exposed in Codex, Claude, Grok, and Agy lanes. The
operator explicitly asked us not to check MCP pricing. The reviewer prompt
forbids external actions, but MCP tools are not mechanically restricted.

Live Grok MCP smoke: `/tmp/agent-offload/xcheck-v2-mcp-grok-20260927/` ended
`done`; `qmd__query` was called through MCP with no approval prompt. The
first Codex/Agy probe exposed a bad probe prompt and qmd absent from Agy.
The corrected probe is `/tmp/agent-offload/xcheck-v2-mcp-connected-20260927/`:
Codex and Agy both called `semble.search` and ended `done` without approvals.

Continuation verification: `/tmp/agent-offload/xcheck-v2-rule-smoke-20260927/`,
roster `claude-sonnet-5-low` (Codex is the orchestrator, so no Codex peer).
From sandboxed `codex exec`, run absolute
`/Users/yarco/.local/bin/xcheck run --base /tmp/agent-offload/xcheck-v2-rule-smoke-20260927 --orch codex --lanes claude:claude-sonnet-5:low --mode headless --skip-capacity --detach`,
then absolute `xcheck wait` with the same BASE and `--timeout 120`.
The first launch had no spend because the model appended `.` to both commands;
the corrected run launched, but the peer copied the entire proof file and was
`invalid`. Retry with an exact one-line proof prompt in
`/tmp/agent-offload/xcheck-v2-rule-smoke-2-20260927/`, same roster and commands
with the new BASE.

Denial matrix, controlled harness test (`--orch other` permits the Codex
adapter; these are not independent review peers):

- `/tmp/agent-offload/xcheck-v2-denial-stream-20260927/`:
  `xcheck run --base /tmp/agent-offload/xcheck-v2-denial-stream-20260927 --orch other --lanes codex:gpt-6-sol:high,claude:claude-sonnet-5:high,grok:grok-4.7:low --mode headless --view stream --cap 600`
- `/tmp/agent-offload/xcheck-v2-denial-tui-20260927/`:
  `xcheck run --base /tmp/agent-offload/xcheck-v2-denial-tui-20260927 --orch other --lanes codex:gpt-6-sol:high,claude:claude-sonnet-5:high,grok:grok-4.7:low,agy:gemini-3.8-flash-high --mode headless --view tui --cap 600`

The first matrix packet accidentally specified `RESULT_COMPLETE.` with a
period, making the Codex answers invalid. Fresh packet retries, Codex only:
`/tmp/agent-offload/xcheck-v2-denial-codex-{stream,tui}-retry-20260927/`;
for each BASE: `xcheck run --base BASE --orch other --lanes
codex:gpt-6-sol:high --mode headless --view <view> --cap 300`.

Grok Bash adapter smokes (controlled `--orch other`, one Grok lane each):
`/tmp/agent-offload/xcheck-v2-grok-bash-{stream,tui}-20260927/`;
for each BASE: `xcheck run --base BASE --orch other --lanes
grok:grok-4.7:low --mode headless --view <view> --cap 300`.

Verified live on this machine today: headless stream runs (all 4 harnesses),
Herdr TUI runs of all six default lanes with ZERO manual input (trust recorded
automatically, no dialog on any screen), a sandboxed `codex exec` orchestrating
real headless and Herdr runs through the Codex rule with no escalation,
pool-lock queueing across runs, crash settlement.

Claude was below the 10 % quota reserve during the denial matrix, so its
stream and TUI denial cases remain unverified. Do not force it without the
operator's say-so. Real quota/auth streams also remain uncaptured.

## P0 — completed fixes (historical checklist)

1. **`xcheck/validate.py`: accept `*` list bullets on the PROOF line.**
   `_proof` and `_substantive` use `(?:[-+]\s+)?`; `_heading_re` uses
   `(?:[-+*]\s+)?`. `* **6) PROOF:** <token>` is rejected today. Change both to
   `[-+*]`, and add `* **6) PROOF:** {token}` and `* 6) PROOF: {token}` to the
   PROOF strategy in `tests/test_laws.py` (negative control: wrong token stays
   invalid). Source: agy-flash verification review.
2. **`xcheck/files.py:is_alive`: probe with `LOCK_SH | LOCK_NB`, not `LOCK_EX`.**
   Two concurrent probes of a dead holder make one report "alive". Test: two
   threads probe an unlocked file → both False; a held `LOCK_EX` → True.
3. **`xcheck/install.py`:** (a) delete `prune()` and its call in `bin/xcheck`
   (it can delete a snapshot a queued run is about to exec, and it crashes on
   the `current` symlink); (b) Codex rule: drop the bare `["xcheck"]` pattern
   (PATH hijack) and allow only the absolute `~/.local/bin/xcheck` with the
   public subcommands `run`, `wait`, `status`, `doctor` (never `lane`,
   `install`); verify end to end from a sandboxed `codex exec` (see note's
   Codex paragraph); (c) record the source folder path at install
   (e.g. `~/.local/share/xcheck/source`) and make `doctor` compare the
   installed digest with that source's `install.digest()` even when run from
   the installed copy (today it prints ok blindly).
4. **`xcheck/capacity.py:_probe`**: run the helper with `sys.executable, "-I"`.
5. **`xcheck doctor` flag pins**: confirm every pin in `adapters.HELP_COMMANDS`
   appears in the installed help (`--no-auto-update` was removed for this
   reason; Grok memory/updater are env vars now). Add a unit test feeding
   captured help text (negative control: removing `--sandbox` fails).
6. **Delete `tools/read-depth-probe`** (gameable by grep; its result — Agy
   reaches every byte of ~1 MB through tools — is recorded in the note). Remove
   note references.
7. **Update the note and ledgers for commits 2d1efec..22b8db1** (the note is
   partly stale):
   - I3 wording: Claude is tool-confined; Grok MCP servers start but
     `use_tool` is denied; Agy MCP/web limits are instruction-only.
   - Agy: always its app, headless too (`adapters.APP_ONLY`), because print
     mode ends the turn when it soft-denies a tool confirmation (evidence:
     verification lane `agy-claude-sonnet-4-6`, agy-cli.log
     `Print mode: soft-denying tool confirmation "RunCommand" at step 58`
     after `SANDBOX_COMMAND_BLOCKED`). Correct the Agy bullets that say print
     mode is fine.
   - Per-lane `lanes/<lane>/harness.log` (agy `--log-file`, claude/grok
     `--debug-file`); update-prompt switches (Codex
     `-c check_for_update_on_startup=false`, `GROK_DISABLE_AUTOUPDATER=1`,
     `DISABLE_AUTOUPDATER=1` for Claude); Codex app web = `--search`.
   - Stable lane workspaces `ROOT/.lanes/<lane>` + `xcheck/trust.py`
     (Codex `[projects."<dir>"] trust_level="trusted"`, Agy
     `trustedWorkspaces` + `showFeedbackSurvey=false`, both `/tmp` and
     `/private/tmp` spellings); `lanes/<lane>/staged` stamp guards salvage.
   - Pool lock inherited by the harness (orphan keeps its pool; next run
     queues); `end_group` TERM→3 s→KILL; stderr no longer resets idle; idle
     default 180 s for apps, 900 s for streams; every `*_API_KEY` scrubbed;
     exit 3 prints the status table; `wait` takes over a dead supervisor with
     its lock; watchdog from `run.epoch`.
   - macOS fact: `killpg` returns EPERM for an exited-unreaped leader and for
     groups with an unsignallable member (`files.signal_group` handles it).
8. **Stale trust entries** (old per-run paths) in `~/.codex/config.toml`
   (`[projects."/…/agent-offload/<run>/…"]` blocks, not `/.lanes/`) and in
   `~/.gemini/antigravity-cli/settings.json` `trustedWorkspaces` (44 entries,
   including a bare `/tmp/agent-offload`). They were written by v1 helpers and
   two manual Enter presses. The operator approved removal; only those
   entries were removed, and every `/.lanes/` entry was kept. The old per-run
   directories still existed at cleanup time, contrary to the earlier note.

## P1 — prove there are no other "dropped safeguard" failures

9. **Denial matrix, empirically.** For every harness × view (codex/claude/grok
   stream + tui; agy tui), stage a packet whose PROMPT requires, before
   answering: (a) try to write a file outside ANSWER.md, (b) run a shell
   command, (c) one web search, (d) read a file outside the workspace. Pass =
   the lane still ends `done` with a valid answer. This is exactly the class
   that killed the Agy print-mode lane; it is untested for the others.
10. **Real quota/auth deaths are unverified.** Structured errors were only
    tested with invalid-model calls (fixtures). Safety holds without it (a
    non-answer can never validate; nothing retries), but say so in the note,
    and capture a real quota stream the next time one occurs.
11. **Re-mine `../x-check-old/Herdr Host.md`** for the TUI path line by line
    (it was marked "TUI-only, omitted" before v2 adopted TUIs). Already
    covered: HERDR_* scrub, trust, Agy survey, prompt as argv (no swallowed
    prompt), history persistence, accept-edits/skip-permissions, never close
    caller pane, ownership check. Check anything else (e.g. account
    verification splash timing vs the 180 s app idle).
12. **Grok residual**: `--sandbox read-only` allows writes anywhere under
    `/tmp`, including other lanes' files; its TUI needs `/tmp` for ANSWER.md.
    Decide: accept (document) or a custom profile denying other paths.

## P2 — verify and polish

13. Write `/tmp/agent-offload/xcheck-v2-verify-20260927/CONSENSUS.md` from the
    adjudication below (tmp only; never copy run artifacts into the vault).
14. Run a follow-up v2 cross-check on the final tree with `xcheck` itself:
    stage the repo, `../x-check-old/` for gap checks, the three ledgers
    (regenerate from this file + git log if /tmp was cleared), and both
    rounds' reviews if still present. Ask peers to attack decisions, not
    re-report documented omissions.
15. Consolidated first-principles design pass with online research
    (inspiration found so far: llm-council's anonymized peer ranking and
    chairman synthesis; agent-manager / Herdr's own hook-based agent state;
    Ordewell's "completion marker, never model self-grading").

## Verification round adjudication (BASE /tmp/agent-offload/xcheck-v2-verify-20260927)

Lanes: codex-gpt-6-sol-high done, grok-4.7-xhigh done, agy-gemini-3.1-pro-high
done, agy-gemini-3.8-flash-high done, agy-claude-sonnet-4-6 invalid (print-mode
soft-deny; root cause above).

Adopted and DONE (22b8db1): harness inherits pool lock (codex); group
TERM→KILL (codex, pro, grok); durable pane ids as created (codex, grok,
flash); lane-name validation (codex); over-cap runner stopped not abandoned
(codex, grok); clear print instruction in lane message (grok); `.launched`
released if setup fails (grok); exit 3 prints table (grok); unused
`Run.skipped` removed (grok); boundary-matched orphan identity (grok);
stderr no idle reset (flash); `*_API_KEY` scrub (flash); `wait` takes the
supervisor lock (flash); watchdog from launch (flash); `_Stop` during cleanup
still writes exit.json (pro); view.log fd closed (flash).

Adopted and PENDING: P0-1 `*` bullets (flash), P0-2 `LOCK_SH` probe (flash),
P0-3 install/rules/doctor (codex, grok, flash), P0-4 `-I` (grok), P0-6 delete
probe tool (pro), P0-7 I3 wording and Claude web tools in the note (grok, codex).

Rejected, with reasons: isolated HOME for Agy (breaks auth; pro); "PROOF uses
substring match" (false: exact equality; pro); remove Claude `--allowedTools`
(it pre-approves, `--tools` only lists; flash); kill orphans without identity
when `ps` is denied (PID reuse; flash); shell-readiness wait before
`pane run` (typeahead is buffered; ~25 live launches never missed; a miss shows
as `failed`; grok); loader-variable scrub (the Codex rule matches only the
exact command; grok); `try/finally` around pool release (process exit releases
it; flash).

## Evidence

Durable copy (git-ignored, survives reboot): `tmp/handoff-evidence/` —
`round1/` (plan, five blind designs + critiques, consensus), `verify/` (five
verification reviews incl. the invalid Sonnet answer and its agy-cli.log),
`ledger/` (REQUIREMENTS, DECISIONS D1–D26, GAPS), `measurements/`
(M1–M13), `cli-ref/` (installed CLI help). Original run folders (may vanish on
reboot):

- Round 1 design (blind designs + critique of the plan):
  `/tmp/agent-offload/xcheck-v2-design-20260927/` (RESULT-*.md, CONSENSUS.md,
  evidence/plan/PLAN-claude-opus-5-5.md).
- Verification round: `/tmp/agent-offload/xcheck-v2-verify-20260927/`
  (RESULT-*.md, lanes/*/stream.jsonl, agy-cli.log; ledgers in
  `evidence/ledger/` REQUIREMENTS.md, DECISIONS.md D1–D26, GAPS.md;
  `evidence/measurements/MEASUREMENTS.md` M1–M13).
- Live smokes: `/tmp/agent-offload/v2-tui-all-*` (six TUIs, zero input),
  `v2-tui-rest-*` (the EPERM crash), `v2-codex-orch*`, `agy-depth-*`.

## Gotchas

- Dropbox's File Provider resets a NEW file to mode 600 after first sync:
  `chmod 755` executables again (bin/xcheck, tests/fake/*).
- Tests set `XCHECK_ROOT`, `CODEX_CONFIG`, `AGY_SETTINGS` to temp paths; never
  point tests at the real `/tmp/agent-offload` (they would queue behind real
  runs' pool locks) or the real configs.
- Run the suite in the background (~100 s); zsh globs `====` in echo strings.
- Herdr mode needs `HERDR_ENV=1`, `HERDR_PANE_ID`, tab ≥ 176×14.
- Commit messages: facts and invariants, no dated war stories (operator
  preference); do not copy run artifacts into the vault.
