# Grok ACP lane

xcheck's Grok lane runs Grok's own documented ACP stdio server under acpx's
generic custom-agent transport. There is no built-in Grok adapter in acpx and
no private ACP client, wrapper, or protocol workaround anywhere in the path.

Historical qualification: **grok 1.0.62 (1474f9cbdd91)** with acpx 0.19.6. The 2026-10-17 probe spoke to `grok agent stdio` directly. These versions date the observations below; they are not current runtime admission pins.

## Current admission contract

`xcheck/grok.py` serves both lane admission and `doctor`. It first runs the
configured `version --json` startup, then drives the lane's own agent command
through `initialize` and `session/new`, never `session/prompt`. It verifies the
resolved tool allowlist, zero loaded hooks, approved MCP names and selected
model/effort from the probe log and native session result. Unknown or mismatched
evidence refuses inference; a version change alone does not. The probe reads raw
chunks so multiple JSON lines written together cannot stall in a buffered
`readline` invisible to `select`.

The rationale is native fail-open behavior: an unknown tool ID expands the
toolset. Keep this behavior gate until the native allowlist fails closed and an
equally observable replacement is verified. Regression evidence lives in
`tests/test_runner_issues.py`; live tool qualification remains necessary after
changes to transport or permissions. Launch construction stays in
`adapters._grok_launch`; admission does not own reviewer execution or completion.

This is a version-specific verification record. Runtime pins and argv live in `xcheck/adapters.py`; this launch sketch does not replace them. Recheck native startup, tools and event shapes after upgrades. Current admission comes from the installed `xcheck doctor` and lane preflight, not this recorded result. The [protocol](../../Multi-Agent%20Cross-Check%20Protocol.md) owns shared preconditions, interaction paths and invariants.

The 1.0.60 zero-turn startup probes (2026-10-16) retained six native tools in
packet mode (four allowed tools plus the two MCP dispatch tools), and eight in
live mode, as 1.0.52 did. No operator hooks loaded or ran (`total_hooks=0`).
Live headless and Herdr qualification runs exercised shell, native web fetch
and read-only qmd MCP and reached native completion (`end_turn`, `done`).
1.0.60 adds more `_x.ai/*` notifications to the stream (queue, settings,
announcements, `session/prompt_complete`); the reader ignores them and still
settles on the `session/prompt` response. The CLI binary is the upstream
release; only xcheck’s admission pin changed.

The 1.0.62 zero-turn probes (2026-10-17) kept the declared allowlist,
reported zero loaded hooks and only approved MCP servers, and selected the
requested model and effort. Native MCP dispatcher tools and approved registered
MCP tools can increase the displayed count after initialization; the count alone
is not an allowlist check. `_x.ai/session/setup`, `models/update`,
`mcp/init_progress` and `mcp_initialized` carry no `stopReason`; the reader
ignores them for completion.

## Launch shape

`adapters._grok_launch` builds one argv per lane:

```
acpx --agent "<python> -I bin/xcheck hold --pool grok --run <base> --log harness.log
              -- grok agent --always-approve --no-leader -m grok-4.7
                 --reasoning-effort xhigh --agent-profile grok-profile.md
                 --debug-file grok-agent.log stdio"
     --cwd <workspace> --model grok-4.7
     --approve-all --auth-policy skip --no-terminal --no-fs
     --prompt-retries 0 --format json
     exec --config-option reasoning_effort=xhigh --file <message>
```

- `xcheck hold` joins the lane to the `grok` pool busy-fd before the agent
  starts, the same custody every other ACP lane takes. The shared runner owns
  the process group, cancellation/escalation, `exit.json`, and the stream.
- `--always-approve` resolves Grok's own tool-permission loop inside the
  agent; `--approve-all` answers any `session/request_permission` that still
  reaches acpx. Neither grants anything the profile does not allowlist.
- `--model` is pinned three ways: the agent's `-m`, acpx `--model`
  (`session/new` model selection), and `--config-option reasoning_effort=…`
  plus the agent's `--reasoning-effort` and the lane config's
  `[models] default*`. The advertised `currentModelId`/`reasoning_effort` in
  `session/new` confirms `grok-4.7`/`xhigh`.
- `--no-terminal`/`--no-fs` advertise no client fs/terminal capability; Grok's
  own `run_terminal_cmd`/`read_file` tools run in-agent under the reviewed
  profile instead.

## Agent profile and tool allowlist

`_grok_profile` writes `grok-profile.md` beside the lane's `harness.log`:

```yaml
---
name: xcheck-review
description: Review-only agent profile for multi-model cross-check lanes.
prompt_mode: full
agents_md: true
tools: [read_file, grep, list_dir, run_terminal_cmd, web_search, web_fetch]
---
```

`tools:` are Grok's **internal tool ids**, the same names the headless
`--tools` flag took. This matters: an unknown id makes the allowlist **fail
open** to the full toolset — the doc's example names (`grep_search`,
`run_terminal_command`, `bash`) do exactly that. Check the declared and resolved tool identities, loaded hooks and MCP server
names, not a fixed total: native MCP dispatchers and approved registered MCP
tools can increase the count during initialization. Do not rename entries
without a zero-turn probe. When the run disables web, the two web ids are dropped and
`disable_web_search = true` + `features.web_fetch = false` close the surface.

`run_terminal_cmd` is the sanctioned shell for a lane that needs disposable
writes; the packet message scopes it. Grok's write/MCP limits remain
instruction/profile controls — **not** a Seatbelt-style OS boundary. The
current `sandbox off` policy is `[sandbox] profile = "off"` in the
generated config — the `agent` subcommand has no `--sandbox` flag and
`GROK_SANDBOX` is scrubbed globally (`SCRUB_NAMES`): an inherited env value
overrides the file (verified: a leaked `GROK_SANDBOX=ws` hard-fails launch),
so the key must stay out of every env, the lane's included.

## Isolated home, filtered MCP, auth custody

Each lane gets `GROK_HOME=<lane>/grok-home` with a generated `config.toml`:

- `mcp_servers` copied **verbatim** from the operator config but only for
  `GROK_MCP_ALLOWED = (qmd, semble, deepwiki)` — local or keyless endpoints.
  An allowlisted entry that grows `headers`/`env`/`env_vars` is dropped, so a
  lane can never inherit a credential it was not designed to hold (keenable,
  parallel-search, perplexity, composio stay out). `session/new mcpServers:[]`
  was probed to be **additive** — config filtering is the only boundary.
- Vendor imports are off: `GROK_{CLAUDE,CURSOR,CODEX}_{MCPS,HOOKS,RULES,
  SKILLS,AGENTS,SESSIONS}_ENABLED=0` and `GROK_MANAGED_MCPS_ENABLED=0` —
  imported integrations can carry credentials, executable hooks and
  orchestrator instructions.
- **Vendor plugins** are a separate path the env kills do not gate: Grok
  scans `~/.claude/plugins` (and the cursor/codex stores), honors each
  vendor's own enabled-state file, and *loads executable hooks* — the
  native plugin-import path was observed loading hooks despite those environment
  switches. There is no
  global off-switch (`plugins.disabled = ["*"]` does not match; verified).
  The generated config enumerates every installed plugin name from each
  store's `installed_plugins.json` into `plugins.disabled`, which stops
  loading (discovery still counts them). `plugins.enabled` stays unset so
  project plugins default off; `[marketplace] sources = []` plus
  `GROK_OFFICIAL_MARKETPLACE_AUTO_REGISTER=0` closes Grok's own marketplace
  path. Residual: a plugin installed after lane build is not enumerated —
  the list is rebuilt on every launch.

  Verify with a zero-turn `initialize` + `session/new` handshake against the
  generated config and environment: no loaded hooks or `hook.run` events,
  and only approved MCP servers. Recheck a completed qualification turn too;
  startup success alone does not prove the whole turn stayed within policy.
- `GROK_AUTH_PATH` points at the operator's real `~/.grok/auth.json`: one
  subscription SessionToken source, no copy (a copy forks token-refresh
  rotation) and no API-key fallback (`GROK_DISABLE_API_KEY_AUTH=1`;
  `*_API_KEY` env is scrubbed anyway). Lane sessions, caches and plugins stay
  inside the lane home — verified: sessions land under `GROK_HOME`, only
  qmd/semble/deepwiki initialize, zero auth errors.
- `[paths] extra_rule_dirs` is carried **verbatim** from the operator config.
  Those configured rules reach the lane by reference, not as copies;
  `extra_skill_dirs` is not carried. Do not depend on vendor compatibility
  imports for rule delivery. The inspected Grok versions did not expose
  `extra_rule_dirs` in `grok inspect`; verify config parity and delivery with
  a synthetic non-sensitive sentinel. This can expose configured instructions
  to the peer, so keep private operational material out of those inputs.
- A nonempty `~/.grok/requirements.toml` is copied into the lane home so
  enterprise pins survive isolation.

`GROK_*` environment is scrubbed per-kind (`SCRUB_KIND`): the lane rebuilds
every Grok knob itself, so operator overlays, `GROK_HOME`, managed-config and
auth-path injection cannot reach it — while non-Grok peers keep inheriting
operator vars they legitimately read.

## Side inference: honest accounting

A fresh Grok session makes one separate **initial title-sampling** attempt.
Paired zero-tool native probes on 1.0.50 (one "Reply with exactly OK" turn,
then a 20 s grace) showed it directly: the control session generated an
automatic title; a session renamed before the prompt kept its manual title
and still logged a generated title, discarded, after the answer. Both
reported `usage.json modelCalls=1`. So `modelCalls` counts the main loop
only, not the title call, and no title call can be derived by subtracting 1
from it. Treat `usage.json`/`summary.json` counters as reported metrics,
never as a complete inference or billing total; auxiliary failures and
retries can change sampling too.

The title attempt has no supported off-switch. The public vendor extension
`_x.ai/session/rename` (`sessionId`, `title`, optional `cwd`/`kind`/
`resetToAuto`) is accepted before a prompt and persists
`title_is_manual:true`, but on a fresh resident session it only freezes
later refresh: the initial generator still samples and discards. Do not add
a pre-prompt rename, seed native storage or patch Grok to save this call.
The documented session `_meta` keys (`rules`, `systemPromptOverride`,
`agentProfile`, `yoloMode`, `autoMode`) do not disable it either.

Everything else is off and verified absent: `GROK_TITLE_REFRESH=0` +
`features.title_refresh=false` stopped the early-turn title refresh,
`GROK_TURN_SUMMARY`/`GROK_SESSION_RECAP`/`GROK_SESSION_SEARCH`/
`GROK_MEMORY`/`GROK_SUBAGENTS` are `0`/`false`, the profile excludes goal
tools and the debug log shows no active goal. That is a statement about the
observed lanes, not a claim that every auxiliary mechanism is disabled.

## Failure and completion semantics

The shared ACP reader (`adapters.Reader`) settles the turn on the
`session/prompt` response, recognised by its `stopReason` (only a prompt
response carries one, so id reuse by the agent cannot hide it): `end_turn`
completes, any other reason fails. Request ids from both directions are
counted until answered; an error carrying the prompt id while another
request with that id is open is held until the remaining response at that id
resolves which side sent it, or until acpx exits without completion. Grok
additionally surfaces `_x.ai/mcp/server_status` notifications as
`mcp_startup` diagnostics when a server is not `ready`.

`xcheck doctor` runs the same admission as a lane: the native startup probe
— `grok --sandbox off --cwd <tmp> version --json` (zero inference, sandbox
applied) — then the resolved-toolset contract below, plus flag drift checks
for `grok agent --help` and `acpx`.

## Maintenance

- **Behavior admission gate**: `lane.grok_admission_check` replaces the old
  `GROK_VERIFIED_VERSION` pin — Grok revs too fast for an exact-version gate.
  Before a lane spends anything, the adapter extracts the inner `grok agent
  … stdio` argv from the real `--agent` payload (`adapters.grok_probe_command`),
  repoints `--debug-file` at a sibling `grok-admission.log`, and drives the
  process through ACP `initialize` + `session/new` — **never
  `session/prompt`**, so the check is zero-inference. It then asserts the
  contract the run actually depends on, from the profile, the session/new
  result and the debug log (`lane._grok_contract`):
  - the profile's declared `tools:` list is exactly a review toolset
    (`GROK_REVIEW_TOOLS`, plus `GROK_WEB_TOOLS` only when web is enabled);
  - the debug log's `tools allowlist applied agent=xcheck-review` resolves
    to the same set — a renamed tool id makes the allowlist fail OPEN to
    the full toolset, so resolved must equal declared, not just contain it;
  - `total_hooks=0`;
  - every `config_names` MCP entry is inside `GROK_MCP_ALLOWED`;
  - the selected model equals the lane's `-m` pin and
    `reasoning_effort` equals its `--reasoning-effort` pin.
  Any drift skips the lane with `native-unverified: grok <version> contract:
  <what drifted>` — the report names the drift, never the version. An
  unobservable contract (no allowlist line, no hook count, no MCP line in
  the log) fails closed rather than admitting an unknown toolset. Routine
  upgrades pass automatically while the behavior holds; the version string
  is diagnostic only.
- If Grok renames tool ids, `_grok_profile`'s allowlist fails open — the
  profile test pins the exact list, and the admission gate catches the
  drift per-lane anyway. If Grok stops logging `tools allowlist applied`
  or `total_hooks`, the gate fails closed with an "unobservable" diagnosis
  — re-verify against the new build before relaxing the check.
- If the `grok agent … stdio` command shape inside `--agent` changes,
  `grok_probe_command` returns `None` and the lane reports
  `native-unverified`; update the extraction and re-validate.
- If acpx gains a session-metadata feature, nothing here needs it: the
  profile reaches Grok through the native `--agent-profile` flag inside the
  `--agent` command.
- If the operator config gains a new keyless local MCP, add its name to
  `GROK_MCP_ALLOWED` deliberately; anything credential-bearing or billed
  stays out.
