# Reviewed Codex ACP automation build

xcheck's Codex lane requires a locally maintained build of
[`@agentclientprotocol/codex-acp`](https://github.com/agentclientprotocol/codex-acp),
based on **v2.1.1**, commit `68d7d2d5ddfc0ed5746f9f6130892dda685e65dd`.
This is not an upstream release. Installing stock npm 2.1.1 does not satisfy the lane.

The patch belongs at the native-to-ACP boundary and changes three behaviors:

- A native failed turn returns a JSON-RPC error to ordinary ACP clients, even
  when they do not negotiate JetBrains' typed session-failure extension.
- `CODEX_ACP_EPHEMERAL=1` starts an ephemeral Codex thread.
- The same opt-in suppresses the adapter's separate post-turn title inference.

Persistent sessions retain their existing title behavior. Existing auth and
negotiated typed-failure behavior remain supported. `--automation-capabilities`
is a local build marker, not an ACP standard or an upstream supported flag.
It prints the exact version and the two required capabilities without launching
Codex. xcheck probes it with `CODEX_PATH=/dev/null`, so even an unknown flag on
an unpatched adapter cannot launch its bundled Codex. A missing, stock or changed
build skips the lane before admission and model spend.

The patch uses zero context so its blank context lines do not become trailing
whitespace in the containing repository. It is tied to the exact commit below;
apply it with `--unidiff-zero` after checking that hash.

## Reproduce

From the xcheck repository, capture the absolute patch path, then build in a
scratch directory outside the vault:

```sh
patch_file="$PWD/integrations/codex-acp/automation.patch"
build_dir=$(mktemp -d /tmp/codex-acp-build.XXXXXX)
git clone --branch v2.1.1 --depth 1 https://github.com/agentclientprotocol/codex-acp.git "$build_dir"
cd "$build_dir"
test "$(git rev-parse HEAD)" = 68d7d2d5ddfc0ed5746f9f6130892dda685e65dd
git apply --check --unidiff-zero "$patch_file"
git apply --unidiff-zero "$patch_file"
npm ci --ignore-scripts
npm run typecheck
npm test
npm run build
node dist/index.js --automation-capabilities
npm pack --ignore-scripts
```

Required output:
`{"version":"2.1.1","ephemeralSessions":true,"failedTurnErrors":true}`.
The upstream suite includes skipped credential-dependent E2E tests; xcheck's
controlled subscription smokes test the actual installed operator Codex instead.

## Install after operator approval

Keep this build in its own prefix rather than the global npm prefix: topgrade
runs global `npm update`, which could replace a globally installed local build.
With the archive produced above:

```sh
prefix="$HOME/.local/share/xcheck-codex-acp/2.1.1-automation.1"
mkdir -p "$prefix"
npm install --prefix "$prefix" --ignore-scripts "$build_dir/agentclientprotocol-codex-acp-2.1.1.tgz"
# First inspect an existing ~/.local/bin/codex-acp; do not overwrite another install.
ln -s "$prefix/node_modules/.bin/codex-acp" "$HOME/.local/bin/codex-acp"
codex-acp --automation-capabilities
# From the xcheck source repository:
bin/xcheck install
xcheck doctor
```

The adapter still declares a bundled Codex dependency, but xcheck sets
`CODEX_PATH` to its installed internal app-server entry point, which executes the
operator's actual Codex CLI under the existing pool hold. No separate credentials
or `CODEX_HOME` are created. The chain is acpx → hold → codex-acp → xcheck
app-server/hold → installed Codex app-server.

## Maintenance

No updater or global npm registration is added. This patch and reviewed version
are an explicit maintenance obligation. For an upgrade, inspect native sandbox,
approval and failure translation; apply or retire each patch against its upstream
behavior; run the adapter suite, xcheck suite, no-inference failure probe and both
placement smokes; then change `CODEX_ACP_VERSION` and its fake capability fixture.
Do not loosen the marker guard, use acpx's built-in Codex name, or infer success
from transcript text. A protocol- or version-changing package stops before spend
until deliberately re-verified.
