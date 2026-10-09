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
`cancel`, `doctor`, `capacity`, `models`) without a sandbox prompt. Codex, Agy, Devin and OpenCode lanes need `acpx`
(`npm install -g acpx`) and an ACP server (Devin/OpenCode's `acp` subcommands, Antigravity's separately installed server, or the
[reviewed Codex adapter build](integrations/codex-acp/README.md)). Stock `codex-acp` is insufficient; the local patch has an explicit maintenance cost. Codex shows its stream in both placements, and no longer edits workspace trust entries.
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
uv run --with 'hypothesis>=6.100' python3 -m unittest discover -s tests   # a few minutes
git diff --check
```

Optional long acceptance (185 s):

```bash
uv run --with 'hypothesis>=6.100' python3 -m unittest discover -s tests -p acceptance_runtime.py
```

## License

[MIT](LICENSE).
