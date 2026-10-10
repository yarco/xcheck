# xcheck — Multi-Agent Cross-Check

Fan one immutable review packet out to **other** agent CLIs (Codex, Claude Code, Grok, Agy, Devin, OpenCode) as real processes, collect one answer file per lane, and leave judgment to the orchestrating model.

- One operator, one macOS machine, attended runs.
- The code owns everything with one mechanical right answer: exact argv, process groups, deadlines, capacity, locks, placement, stream parsing, validation, status.
- The orchestrating model owns judgment: roster, packet, diagnosing a failed lane, adjudication, `CONSENSUS.md`.

The full protocol — packet contract, lane statuses, placement and views, capacity, invariants, merge rules — lives in [Multi-Agent Cross-Check Protocol.md](Multi-Agent%20Cross-Check%20Protocol.md).

## Install

```bash
bin/xcheck install   # publishes the read-only, content-addressed copy to ~/.local/bin/xcheck
xcheck doctor        # verify the installed copy, flags, and model pins
```

`install` also writes `~/.codex/rules/xcheck.rules`, which lets a Codex
orchestrator run the installed `xcheck` (`run`, `wait`, `status`, `activity`,
`cancel`, `doctor`, `capacity`, `models`) without a sandbox prompt. Codex lanes run the installed `codex exec --json`.
The other lanes are ACP sessions: Grok, Agy, Devin and OpenCode need stock `acpx` (`npm install -g acpx`), Claude the
[reviewed `xcheck-acpx` build](integrations/claude-acp/README.md), and each harness its ACP server (`grok agent stdio`,
Devin/OpenCode's `acp` subcommands, Antigravity's separately installed server, stock `claude-agent-acp`). Every lane shows
its stream in both placements. This is the `acp/claude` branch, which keeps the Claude ACP lane; `main` runs Claude on
`claude -p --output-format stream-json`, and the Codex ACP lane is on `acp/codex` (see the protocol note).
Herdr placement requires the operator's `~/.agents/bin/herdr-caller-context` helper; missing or unverified context falls back to headless in auto mode. Lane shells run
through your zsh rc files; if
yours load secrets, they must skip that load when the lane guard variable is
set, or keys will reach peers with shell access (the protocol note covers the
re-verification).

Requires Python 3.12+ (stdlib only at runtime) plus the harness CLIs you plan to use as peers. Hypothesis is needed for the test suite only.

## Run

```bash
xcheck run --base /tmp/agent-offload/<slug> --orch <your-harness>
```

Stage `BASE/PACKET.md`, `BASE/PROMPT.md`, and `BASE/evidence/` before launching. See the protocol note for the packet contract, the answer grammar, and the merge rules.

## Tests

```bash
uv run --python '>=3.12' --with-requirements requirements-test.txt pytest   # ~25 s, all cores (pytest.ini)
git diff --check
```

Optional long acceptance (185 s):

```bash
uv run --python '>=3.12' --with-requirements requirements-test.txt pytest tests/acceptance_runtime.py
```

## License

[MIT](LICENSE).
