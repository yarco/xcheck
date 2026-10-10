# Grok ACP lane

xcheck's Grok lane runs Grok's own documented ACP stdio server under acpx's
generic custom-agent transport. There is no built-in Grok adapter in acpx and
no private ACP client, wrapper, or protocol workaround anywhere in the path.

Verified with **grok 1.0.50 (c58f321264ba)** and **acpx 0.19.4**.

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
`run_terminal_command`, `bash`) do exactly that. The lane's debug log records
`tools allowlist applied` with `tool_count=8` (6 allowlisted + Grok's
`search_tool`/`use_tool` MCP meta-tools). Do not rename entries without a
zero-turn probe. When the run disables web, the two web ids are dropped and
`disable_web_search = true` + `features.web_fetch = false` close the surface.

`run_terminal_cmd` is the sanctioned shell for a lane that needs disposable
writes; the packet message scopes it. Grok's write/MCP limits remain
instruction/profile controls — **not** a Seatbelt-style OS boundary. The
authorized `sandbox off` policy is `[sandbox] profile = "off"` in the
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
  vendor's own enabled-state file, and *loads executable hooks* — a live
  lane showed `plugin/warp post_tool_use` hooks firing. There is no global
  off-switch (`plugins.disabled = ["*"]` does not match; verified). The
  generated config enumerates every installed plugin name from each
  store's `installed_plugins.json` into `plugins.disabled`, which stops
  loading (discovery still counts them). `plugins.enabled` stays unset so
  project plugins default off; `[marketplace] sources = []` plus
  `GROK_OFFICIAL_MARKETPLACE_AUTO_REGISTER=0` closes Grok's own marketplace
  path. Residual: a plugin installed after lane build is not enumerated —
  the list is rebuilt on every launch.
- `GROK_AUTH_PATH` points at the operator's real `~/.grok/auth.json`: one
  subscription SessionToken source, no copy (a copy forks token-refresh
  rotation) and no API-key fallback (`GROK_DISABLE_API_KEY_AUTH=1`;
  `*_API_KEY` env is scrubbed anyway). Lane sessions, caches and plugins stay
  inside the lane home — verified: sessions land under `GROK_HOME`, only
  qmd/semble/deepwiki initialize, zero auth errors.
- A nonempty `~/.grok/requirements.toml` is copied into the lane home so
  enterprise pins survive isolation.

`GROK_*` environment is scrubbed per-kind (`SCRUB_KIND`): the lane rebuilds
every Grok knob itself, so operator overlays, `GROK_HOME`, managed-config and
auth-path injection cannot reach it — while non-Grok peers keep inheriting
operator vars they legitimately read.

## Side inference: honest accounting

A controlled smoke measured `usage.json modelCalls=2` for one review prompt
that needed no tool iteration:

1. the review call, and
2. Grok's **initial title-generation** call.

`modelCalls` counts every sampler invocation — a real review packet that
iterated the tool loop logged `modelCalls=21` (20 tool-loop steps + the same
+1 title call). The constant overhead is +1 per session, not per call.

Everything else is off and verified absent: `GROK_TITLE_REFRESH=0` +
`features.title_refresh=false` stopped the early-turn title refresh (old
headless lanes show `title_refresh_idx`; the ACP lane has none),
`GROK_TURN_SUMMARY`/`GROK_SESSION_RECAP`/`GROK_SESSION_SEARCH`/
`GROK_MEMORY`/`GROK_SUBAGENTS` are `0`/`false`, and the debug log shows no
goal/dream/background activity.

The initial title call has **no supported native off-switch**: the documented
`_meta` session options are only `agentProfile`/`yoloMode`, and the one skip
path Grok logs ("session already has a title") requires a manual title that
no ACP method sets for this purpose. This is parity with the retired headless
path, which made the same extra call plus refreshes. xcheck therefore claims
**one** extra model call per review, with `usage.json`/`summary.json` as the
evidence — not zero.

## Failure and completion semantics

The shared ACP reader correlates request ids in both directions
(`Reader._acp`): every request except the client's `session/prompt` is
tracked, a reply to a tracked request is consumed, an untracked RPC error
matching the prompt id is terminal immediately, a `session/prompt` result of
`end_turn` completes the turn, and an acpx-level (id null) error is deferred
to completion. Grok additionally surfaces `_x.ai/mcp/server_status`
notifications as `mcp_startup` diagnostics when a server is not `ready`.

`xcheck doctor` keeps the native startup probe —
`grok --sandbox off --cwd <tmp> version --json` (zero inference, sandbox
applied) — plus flag drift checks for `grok agent --help` and `acpx`.

## Maintenance

- If Grok renames tool ids, `_grok_profile`'s allowlist fails open — the
  profile test pins the exact list; re-probe `tools allowlist applied` in
  `grok-agent.log` after any Grok upgrade.
- If acpx gains a session-metadata feature, nothing here needs it: the
  profile reaches Grok through the native `--agent-profile` flag inside the
  `--agent` command.
- If the operator config gains a new keyless local MCP, add its name to
  `GROK_MCP_ALLOWED` deliberately; anything credential-bearing or billed
  stays out.
EOF