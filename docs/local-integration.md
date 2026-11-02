# Local integration without a private fork

Keep machine configuration outside the repository, or under the gitignored `local/` directory. The public repository contains only placeholder examples. Runtime snapshots contain executable Python files, never local settings, transcripts, native credentials or caller-verification scripts.

## Configuration

Optional `~/.config/xcheck/config.json` is read at each command invocation. `XCHECK_CONFIG=/absolute/path/config.json` selects another file, including a file in `local/`. A missing default file keeps the established defaults. An explicitly selected missing file, malformed JSON, unknown key or invalid value refuses with exit 2 before launch.

```json
{
  "default_lanes": [
    "codex:gpt-6.1-sol:high",
    "claude:claude-opus-5-5:high"
  ],
  "caller_context_helper": "~/.local/libexec/xcheck-caller-context",
  "free_roster": "~/.local/state/xcheck/free-roster.json",
  "agy_rotate_command": "~/.local/bin/agy-rotate",
  "claude_login_command": "~/.local/libexec/xcheck-claude-login",
  "opencode_accounts": {
    "a": "/absolute/path/to/opencode-a.db",
    "b": "/absolute/path/to/opencode-b.db"
  },
  "opencode_lanes_per_account": 5
}
```

All keys are optional. Path values must be absolute or start with `~/`; a helper is an executable path, not a shell command. Lane selectors use the ordinary parser; duplicate normalized names and empty lists are refused. An explicit `--lanes` replaces the configured default roster for that run; `--orch` exclusion still applies. `--add` appends normally. The selected lanes/efforts are frozen in `run.json`. Do not change local integration settings while a run is active.

The helper path and optional roster path preserve the existing default locations when omitted. Existing `XCHECK_AGY_ACP_DIR` and `XCHECK_GROK_HOME` overrides remain supported; they are local installation/test settings, not credentials. Native login stores remain native. Do not put tokens in this JSON or commit an environment dump.

`free_roster` points to an optional JSON object such as `{"lanes":["opencode:provider/model"]}`. Missing/unreadable roster files produce no optional free lanes. The file is a selection aid, not proof of current price, availability or quality. `xcheck models` checks provider sources live; an external qualification job is not bundled. `doctor` checks optional roster pins as well as defaults.

`agy_rotate_command` names an optional executable that owns the operator's Antigravity accounts. When a run reads any selected, non-forced Agy lane below its reserve, the supervisor calls it once, before any lane is admitted, as `<cmd> auto --need <bucket,…> --above <pct>`. The needed buckets are the union over every selected Agy lane (Argon's weekly bucket plus Flash's Gemini 5 h and weekly buckets), and the threshold is the run's Agy reserve. The command keeps the current account when it already covers them, otherwise switches the whole account: the ACP server's Keychain login and the `agy` CLI login that answers `/usage` must change together. Exit 0 means a qualifying account is current. Its exit status and last output lines go to `BASE/agy-rotation.json`. Each lane still reads its own pool at admission, so a rotation that failed, found no account, or was refused because another run's Agy lane held the pool leaves the ordinary `reserve-low` skip. `--skip-capacity` never rotates. Without the key nothing rotates.

`claude_login_command` names an existing executable file, with no arguments or shell expansion. Only a selected, non-forced Claude lane with capacity checking enabled may invoke it, once after the bounded native refresh still leaves `auth-required`. The helper receives the scrubbed subscription environment and no input; its stdout/stderr are discarded. It must own an attended native `claude --safe-mode auth login --claudeai`, wait for completion, exit 0 only on success, and terminate its login when canceled. It must never print or persist OAuth URLs/codes in review artifacts or implement its own token exchange. Success only permits another capacity and native subscription-source check. No helper runs for healthy, reserve-low or unknown capacity, for diagnostic commands, or for inference-stage provider errors (including safeguard refusals). Omission disables attended login, while retaining the one native refresh attempt. An explicit lane `--cap` shares one deadline across pool waiting, Claude capacity probes and authentication, then retains the existing separate review-spend cap; an authentication deadline ends `timeout` before inference. Otherwise sign-in remains pending until completion, observed failure or cancellation. Human authentication is still required, including any native code prompt, in the dedicated window.

The optional macOS example `examples/claude-login-macos.py` is a stdlib-only PEP 723 script run by `uv`. Install a reviewed copy at a stable owned location, for example `install -m 700 examples/claude-login-macos.py ~/.local/libexec/xcheck-claude-login` (create that directory first), then set the key above. It binds the native `do script` result by tty to a newly created Terminal window, refusing an existing window or an unobservable binding, and directs Claude's native browser launcher to Firefox Developer Edition using a private `BROWSER` executable; it never changes the system default browser. Firefox Developer Edition and Terminal automation access must already be available. Native `--safe-mode` keeps custom browser/routing settings out of login, while managed policy still applies. With `XCHECK_AUTH_PARENT_LEASE=1`, the helper also watches stdin for EOF from a dead runner; it must not consume that descriptor as an authentication prompt. An owned watchdog inherits the pool and authentication locks through shutdown, including abrupt runner death, and independently enforces the native refresh limit. A private Unix socket leases the native child to the waiting helper: cancellation, a closed window or parent death ends the owned login without reading PID files or killing another session. A worker arriving after cancellation cannot start a login. Temporary files contain only the browser wrapper and socket, never credentials; they are removed on normal exit/cancellation. An uncatchable kill may leave that scratch directory until OS temporary cleanup. The Terminal profile controls whether a completed window stays visible; a success arriving as the window closes is drained before failure is reported. Headless/SSH hosts without an attended desktop should omit this setting or provide their own reviewed helper. Recheck native browser dispatch and cancellation after CLI/OS upgrades; fixtures cannot certify a live OAuth exchange.

`opencode_accounts` maps account names to OpenCode store paths. Each path is absolute or starts with `~/`. Insertion order is the round-robin order for that machine's Zen lanes (a bare model or `opencode/…`). OpenRouter and Z.ai lanes are not assigned. `opencode_lanes_per_account` is an optional positive integer (default 5) capping how many Zen lanes of one run share an account. Omit `opencode_accounts` to keep the native default store and apply no cap. An empty object is refused, as are two paths that resolve to the same file. A run that needs more Zen lanes than the accounts allow, or whose assigned store file is missing, exits 2 before anything starts: a missing file would be created empty and the lane would run anonymously. The assignment is fixed in `run.json` and shown on that lane's status row. Sign each store in once with `OPENCODE_DB=<store> opencode auth login opencode --standalone --method device`. `opencode auth switch` is machine-global and does not pin a lane. These values are store paths, not tokens. Do not put credentials in this file. `doctor` prints one line per configured account.

## Dedicated free API provider

Kilo is keyless. `xcheck models --provider kilo --json` discovers current eligibility without credentials. Its native configuration and isolated database are generated for each invocation; it does not consume a configured Zen account-store slot. The qualification job alone writes the free roster. AnyRouter was removed on 2026-11-02 because access after its trial requires a paid plan or donated key.

## Herdr caller-context adapter

The helper takes no arguments and writes one JSON object. An unverified result may be as small as:

```json
{"state":"unconfirmed"}
```

A verified binding must have this shape, with identities established from current native-session/topology evidence:

```json
{
  "state": "verified",
  "binding": {
    "pane_id": "example-pane",
    "socket_path": "/absolute/path/to/herdr.sock",
    "session": "example-session",
    "terminal_id": "example-native-terminal"
  }
}
```

Exit 0 is required for a verified result. Other states/exit codes, missing fields, malformed output or a helper timeout cannot establish identity. xcheck then reads that exact socket/session/pane's layout and requires at least 176×14. Auto mode falls back to headless; explicit Herdr mode refuses with the observed reason.

The adapter owns identity verification. A static JSON script is not a valid live verifier. Do not infer identity from focus, titles, cwd, an inherited `HERDR_ENV=1`, or a root session ID shared by a fork. Publish your own verifier separately only after reviewing its machine assumptions. The public runner intentionally does not copy an operator's private verifier.

## Shell and sandbox adapters

A wrapper on PATH must preserve process ownership and inherited pool descriptors, and should `exec` the vendor CLI. Validate cancellation and pool release with the fake/acceptance suite after changing it. A wrapper does not automatically isolate shared configuration or `/tmp` records. xcheck cannot certify a third-party wrapper's sandbox.

The lane environment removes orchestrator identity, routing overrides and general API-key variables, with a narrow selected-provider allowlist for OpenCode. Shell startup files can reintroduce credentials; independently check the environment peers actually receive without printing secret values. Keep private shell-loading rules and host permissions in your own configuration.

## Preserve an existing installation

A clone and `examples/offline_demo.py` do not switch the real installed runtime. Tests set disposable HOME and run roots. To adopt a release privately:

1. Review and merge only the intended runtime changes into your canonical source, preserving concurrent work.
2. Keep local files excluded; check `git status --ignored` and audit the explicit staged file list.
3. Install from that canonical source and run `xcheck doctor`.
4. Qualify changed native adapters with a small complete packet and actual tools in each supported placement. Do not infer live tool compatibility from fixtures alone.

If another session owns uncommitted source changes, leave its installation/source record alone until those changes can be integrated deliberately. Source digest mismatch is a readiness gap, not permission to overwrite them.

### Cline account for free reviewers

Install `cline` through npm and run `cline auth cline` for a native OAuth login. The adapter uses `~/.cline/data/settings/providers.json` for authentication and native refresh, with lane-local sessions/configuration. Custom API keys/base URLs are refused. Cline's free feed is public; no login is needed to discover offers. Discovery does not enable billing. The optional free-roster producer also discovers Cline offers and requires each route's own native file-read/proof check.

On Oracle, use a separate attended native login as `xcheckfree`; do not share the Mac refresh token. `deploy/lh/provision-cline.py` in the companion refresh project encrypts that login and changes its data path to service tmpfs. Native refreshes are re-encrypted at stop. The Mac consumer checks its own CLI/login inventory separately from Oracle proof.
