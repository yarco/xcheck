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
  "agy_rotate_command": "~/.local/bin/agy-rotate"
}
```

All keys are optional. Path values must be absolute or start with `~/`; a helper is an executable path, not a shell command. Lane selectors use the ordinary parser; duplicate normalized names and empty lists are refused. An explicit `--lanes` replaces the configured default roster for that run; `--orch` exclusion still applies. `--add` appends normally. The selected lanes/efforts are frozen in `run.json`. Do not change local integration settings while a run is active.

The helper path and optional roster path preserve the existing default locations when omitted. Existing `XCHECK_AGY_ACP_DIR` and `XCHECK_GROK_HOME` overrides remain supported; they are local installation/test settings, not credentials. Native login stores remain native. Do not put tokens in this JSON or commit an environment dump.

`free_roster` points to an optional JSON object such as `{"lanes":["opencode:provider/model"]}`. Missing/unreadable roster files produce no optional free lanes. The file is a selection aid, not proof of current price, availability or quality. `xcheck models` checks provider sources live; an external qualification job is not bundled. `doctor` checks optional roster pins as well as defaults.

`agy_rotate_command` names an optional executable that owns the operator's Antigravity accounts. When a run reads any selected, non-forced Agy lane below its reserve, the supervisor calls it once, before any lane is admitted, as `<cmd> auto --need <bucket,…> --above <pct>`. The needed buckets are the union over every selected Agy lane (Argon's weekly bucket plus Flash's Gemini 5 h and weekly buckets), and the threshold is the run's Agy reserve. The command keeps the current account when it already covers them, otherwise switches the whole account: the ACP server's Keychain login and the `agy` CLI login that answers `/usage` must change together. Exit 0 means a qualifying account is current. Its exit status and last output lines go to `BASE/agy-rotation.json`. Each lane still reads its own pool at admission, so a rotation that failed, found no account, or was refused because another run's Agy lane held the pool leaves the ordinary `reserve-low` skip. `--skip-capacity` never rotates. Without the key nothing rotates.

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
