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
`cancel`, `doctor`, `capacity`, `models`) without a sandbox prompt. Codex and Claude lanes run the installed CLIs'
own headless streams (`codex exec --json`, `claude -p --output-format stream-json`). Grok, Agy, Devin and OpenCode lanes
are ACP sessions: they need stock `acpx` (`npm install -g acpx`) and each harness's ACP server (`grok agent stdio`,
Devin/OpenCode's `acp` subcommands, Antigravity's separately installed server). Every lane shows its stream in both
placements. The ACP forms of the Codex and Claude lanes are kept on the `acp/codex` and `acp/claude` branches until
upstream fixes what they needed patching for (see the protocol note).
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

Use the [preconditions and source authority](Multi-Agent%20Cross-Check%20Protocol.md#preconditions-and-source-authority) before launch. The installed `xcheck` is the sole supported launcher; leave mode unset for automatic Herdr/headless placement. Native streams are the answer channel; panes display them. Own subagents and direct peer CLI scripts are not cross-check launchers.

| Interaction | Command and purpose |
|---|---|
| Readiness | `doctor`, `models`, `capacity`: inspect current runtime, exact model support, provider offers and quota; no review inference |
| Launch | `run --base B --orch H`: snapshot a fresh packet and start selected peers; `--detach` returns without waiting for settlement |
| Observe/reattach | `activity --base B` for metadata; `status --base B` for lane states; `wait --base B` for completion without relaunch |
| Stop | `cancel --base B [--lane NAME] --reason TEXT`: explicit scoped cancellation; interrupting a waiter only detaches |
| Adjudicate | Read settled results and write `BASE/CONSENSUS.md` under the protocol's [merge rules](Multi-Agent%20Cross-Check%20Protocol.md#merge) |

There is no default review timeout; the one-hour warning is diagnostic. Required defaults survive temporary unavailability and retain their selected effort. Dated verification records and captured fixtures do not establish current availability or pricing. See the protocol's [invariants](Multi-Agent%20Cross-Check%20Protocol.md#invariants) and [routine qualification rules](Multi-Agent%20Cross-Check%20Protocol.md#routine-review-planning-and-qualification).

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
