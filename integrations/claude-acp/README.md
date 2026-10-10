# Reviewed acpx build for the Claude ACP lane

xcheck's Claude lane requires a locally maintained build of
[`acpx`](https://github.com/openclaw/acpx), based on **v0.19.4**, commit
`8e396609238086dee6a407fdb3b3ac46dbdedd70`, published under the binary name
`xcheck-acpx` and version `0.19.4-xcheck.1`. Stock acpx does not satisfy the
lane: it forwards only a fixed subset of Claude session options.

The patch adds one vendor-neutral feature, the global flag
`--session-meta <json-or-file>`:

- A value starting with `{` is inline JSON; anything else is a file path
  resolved against `--cwd`. The parsed value must be a JSON object.
- The object is deep-merged over acpx's generated `_meta` and propagated to
  `session/new`, `session/load` and `session/resume`. Explicit keys win per
  leaf, so `{"claudeCode":{"options":{"settingSources":[]}}}` replaces the
  generated `["project","local"]` while `--model` and friends still apply.
- Malformed input exits with `Invalid session meta: …`. Merge writes use
  `Object.defineProperty` with `Object.hasOwn` reads, so `__proto__` or
  `constructor` keys can never touch prototypes.
- The flag is registered in the raw-argv top-level value-flag scan
  (`cli-core.ts`), so a space-separated `--session-meta <value>` cannot hide
  later flags like `--mcp-config` — a regression the patch's integration test
  pins down.

The Claude lane uses it to carry the controls the native `-p` flags provided:
`extraArgs.restricted` (the real `--restricted`, unchanged), plus
`allowDangerouslySkipPermissions:false` — required because the ACP adapter
passes `allowDangerouslySkipPermissions:true` to the SDK unless explicitly
disabled, and `--restricted` refuses the bypass flag. `settingSources:[]`,
`strictMcpConfig`, `persistSession:false`, `permissionPrompts:"none"`,
`disallowedTools:["mcp__claude_ai_*"]`, the exact `tools`/`allowedTools` sets,
`model`, `effort`, `title` and `settings.permissions.defaultMode` ride the
same channel. Any agent can use the flag (e.g. `_meta.agentProfile`).

The adapter itself is stock `@agentclientprotocol/claude-agent-acp` **0.89.0**
— no adapter patch: every needed control is a supported SDK option, and an
explicit `title` provably suppresses the post-turn title-generation call.

The patch uses zero context so its blank context lines do not become trailing
whitespace in the containing repository. Apply with `--unidiff-zero` after
checking the commit hash below.

## Reproduce

```sh
patch_file="$PWD/integrations/claude-acp/session-meta.patch"
build_dir="$(mktemp -d)"
git clone https://github.com/openclaw/acpx "$build_dir/src"
cd "$build_dir/src"
git checkout 8e396609238086dee6a407fdb3b3ac46dbdedd70   # v0.19.4
git apply --unidiff-zero "$patch_file"
pnpm install --frozen-lockfile
pnpm run check        # format, typecheck, lint, build, coverage — all must pass
pnpm pack             # produces acpx-0.19.4-xcheck.1.tgz
node dist/cli.js --version   # 0.19.4-xcheck.1
node dist/cli.js --help | grep session-meta
```

Reproducibility proof (measured on the reviewed build):

- `dist/cli.js` sha256 `64a5b08ae77e49cd62077a79f696b607e5a0d7bd50d4011e75682b63fedb78de`
- `dist/runtime.js` sha256 `5d011887111d85ecc2e5ae0558fde1e011209ecb7c7c287b7cb5730220de445e`
- tarball sha256 `7d4abe074f7a84e364b3d12df8ea5211dd0e13675b887049612fea9acb2cf6bf`

## Install (private prefix; the operator's `acpx` is untouched)

```sh
prefix="$HOME/.local/share/xcheck-claude-acp/0.19.4-xcheck.1"
mkdir -p "$prefix"
npm install --prefix "$prefix" --ignore-scripts "$build_dir/src/acpx-0.19.4-xcheck.1.tgz"
npm install --prefix "$prefix" --ignore-scripts "@agentclientprotocol/claude-agent-acp@0.89.0"
# First inspect any existing targets; do not overwrite another install.
ln -s "$prefix/node_modules/.bin/acpx" "$HOME/.local/bin/xcheck-acpx"
ln -s "$prefix/node_modules/.bin/claude-agent-acp" "$HOME/.local/bin/claude-agent-acp"
xcheck-acpx --version    # 0.19.4-xcheck.1
claude-agent-acp --version   # 0.89.0
bin/xcheck install
xcheck doctor
```

`--ignore-scripts` is required: the operator's npm blocks install scripts and
acpx's `prepare: husky` is dev-only; the package ships a prebuilt `dist/`.

## Policy boundaries

- `settingSources:[]` excludes user, project and local settings. It does not
  exclude managed policy, which still applies with its normal precedence,
  and it does not stop the CLI reading its global `~/.claude.json` state.
- `tools` sets the built-in catalog, `strictMcpConfig` + `--mcp-config` set
  the MCP registrations, `allowedTools` only pre-approves, and acpx
  `--deny-all` refuses any remaining permission request. Keep all four.
- MCP registrations come from the operator's `~/.claude.json`, validated
  before forwarding (`adapters._local_mcp_ok`): a local reader (`semble`,
  `fff`, `qmd`, `pond`) must be a stdio command whose env holds only cache
  locations (`UV_CACHE_DIR`, `UV_TOOL_DIR`, `NPM_CONFIG_CACHE`), or a
  loopback HTTP URL with no headers; the two web endpoints must be exactly
  `{type, url}` with the pinned URL. Anything else drops that server. A
  trusted name is a decision about the command, not proof it is keyless.
- The lane environment drops every `CLAUDE*` name (the pinned adapter reads
  `CLAUDE_CONFIG_DIR`, `CLAUDE_MODEL_CONFIG` and `CLAUDE_AGENT_*`),
  `MAX_THINKING_TOKENS`, `IS_SANDBOX` and every `ANTHROPIC_*`, then sets its
  own `CLAUDE_CODE_EXECUTABLE`. Admission probes `claude auth status` in that
  same environment and requires a first-party claude.ai login.
- The SDK turns these options into flags on the operator's `claude`, which
  upgrades separately; admission and `doctor` require `claude --help` to
  list `--restricted`, `--strict-mcp-config`, `--mcp-config`, `--tools`,
  `--allowedTools` and `--disallowedTools`.
- Visibility gap: the stock adapter does not forward the CLI's MCP startup
  statuses (failed / needs-auth), so a Claude lane cannot report them.
  The unapproved-call audit (`Reader._claude_mcp_policy`) works per
  `toolCallId` on the adapter's exact tool `name`. Its test fixtures are
  derived from a real native call and the pinned renderer, not a live ACP
  capture of a forbidden success.

## Custody chain

acpx gives the agent command only stdio, so each hop rejoins the run's pool:
`xcheck-acpx → xcheck hold → claude-agent-acp → CLAUDE_CODE_EXECUTABLE shim →
xcheck hold → operator claude`. The generated shim is lifetime-only — it joins
the existing hold and `exec`s the operator's `claude` with argv unchanged; it
also pins the operator binary over the SDK's bundled fallback.

## Maintenance

No updater or global npm registration is added; the pin is an explicit
maintenance obligation. For an acpx upgrade, re-apply or retire
`session-meta.patch`, rerun `pnpm run check`, the xcheck suite, and the
zero-turn/one-turn probes, then bump `ACPX_REVIEWED`. For an adapter upgrade,
re-verify the option mapping (esp. `allowDangerouslySkipPermissions`,
`extraArgs`, `strictMcpConfig`, title suppression and `_auth/status_update`),
then bump `CLAUDE_ACP_VERSION` and the fake-harness versions.
