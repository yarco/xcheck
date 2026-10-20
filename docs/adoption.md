# Adopt the workflow

Start with one peer you already use. Add reviewers when they contribute a distinct capability or failure hypothesis. Several models in one family are correlated; more answers are not automatically more evidence.

## 1. Install the runner

Clone this public repository and use Python 3.12 or newer:

```bash
python3 examples/offline_demo.py
python3 bin/xcheck install
export PATH="$HOME/.local/bin:$PATH"
```

The offline example uses only the repository's fake harnesses, with an empty temporary HOME and a separate run root. It leaves your native subscriptions and installed runtime alone.

The real installer publishes a read-only content-addressed snapshot under `~/.local/share/xcheck/`, points `~/.local/bin/xcheck` at it, and writes the dedicated `~/.codex/rules/xcheck.rules` allow rules. It records the source checkout locally for digest checks. It copies only `bin/xcheck` and `xcheck/*.py`; your configuration, source evidence and transcripts are not installed. Installing a different checkout changes the source record and current snapshot: do that deliberately.

## 2. Choose and prepare peers

| Harness | Transport | Local prerequisites |
|---|---|---|
| Codex | `codex exec --json` | Native CLI and existing ChatGPT login |
| Claude Code | `claude -p` stream JSON | Native CLI and first-party Claude subscription login |
| Grok | Native ACP via acpx | Native CLI, stored login, currently admitted CLI version |
| Agy | Antigravity ACP via acpx | Separately installed server, native Keychain login, `agy` quota meter, macOS Seatbelt |
| Devin | Native ACP via acpx | Native CLI and existing login; opt-in, no xcheck quota meter |
| OpenCode | Native ACP via acpx | Native CLI and selected provider's existing login; opt-in, no xcheck quota meter |

Only ACP lanes need stock `acpx` (`npm install -g acpx`); Codex and Claude do not. Install each vendor CLI/server using its official distribution. xcheck does not install them, log you in, configure subscriptions or enable API billing. Model selectors and version admissions live in `xcheck/adapters.py`; the detailed protocol explains why each control exists.

Run `xcheck doctor` and read every diagnostic. It checks the supported roster, including optional lanes, so a missing unused CLI is a coverage gap rather than evidence that a healthy selected peer cannot run. The selected lane repeats native readiness, exact inventory and (where implemented) capacity checks at admission. Capacity failures are skipped and reported, not silently forced or replaced.

Use `--lanes kind:model[:effort],…` to replace the roster for a run, or the optional [local configuration](local-integration.md) to choose defaults. Use the real `--orch` kind; an explicit same-kind peer is refused. `--add` appends an opt-in lane. Do not reduce effort or substitute a model to hide an access failure.

## 3. Stage a review packet

The working example stages only its public toy source:

```bash
python3 examples/stage_demo.py /tmp/agent-offload/preferences-review-1
```

For your own work, create a fresh directory under `/tmp/agent-offload`:

```text
BASE/
  PACKET.md
  PROMPT.md
  evidence/
    src/example.py
    manifest-tail.txt
```

`PACKET.md` starts with provenance: the operator convened the review and supplied the files. Then state scope, requirements, known limitations and exactly one `Proof target: evidence/manifest-tail.txt`. Stage complete files with their repository-relative paths preserved. Remove author/model labels from staged logs and prior rulings. Audit the evidence for secrets and unrelated private material before launch.

Use [examples/PROMPT.md](../examples/PROMPT.md) as the answer schema. It has exactly one `RESULT_REQUIRE: VERDICT` declaration. After all other staging, write an unpredictable last line in the proof target: `MANIFEST_TAIL_TOKEN: <random hex>`. Symlinks and invalid declarations are refused before spend. The launch captures `.packet`; later input edits cannot change queued reviewers' evidence.

For design comparisons, freeze your plan under a neutral filename. Tell each reviewer to record its own design before opening the candidate. For a follow-up, stage the focused delta and decision lineage; place prior rulings in a separate file to be opened after the reviewer's independent view. This sequencing is an instruction, not access isolation.

## 4. Launch and retain the wait

Example for a Claude orchestrator using an existing Codex subscription:

```bash
xcheck run --base /tmp/agent-offload/preferences-review-1 --orch claude \
  --lanes codex:gpt-6.1-sol:high --web packet
```

Leave placement unset: auto chooses verified Herdr panes when available, otherwise headless. `--mode headless` is an explicit operator choice or placement smoke. The toy packet needs no retrieval. Packet mode is not a firewall; general MCP/shell access differs by lane.

Record BASE, exact roster and command in your task tracker or conversation before launch. Retain one host operation or verified background completion callback. The [Run it table](../Multi-Agent%20Cross-Check%20Protocol.md#run-it) specifies each orchestrator's form. Host tools vary: a `.done` file does not itself resume a model. If your host cannot retain a wait or callback, detach and disclose collection pending. Do not promise automatic return or schedule repeated master-model status turns.

`run --detach` starts collection; `wait --base B` reattaches without relaunching. No default timeout is inferred from silence. An explicit `--cap` bounds admission and work; `--idle` can kill a working silent model. Use either only as an operator-selected cancellation policy. Interrupting the subscriber detaches; use `cancel --reason` to stop owned lanes.

## 5. Read and adjudicate

After settlement, reread `STATUS.json` and all `RESULT-*.md`, including invalid answers. Native completion is required before an answer can be published. A complete-looking draft cannot override failure, interruption or an explicit timeout.

| Lane state | What it establishes |
|---|---|
| `done` | Natively completed final message passes the heading/body/proof/marker contract |
| `invalid` | Captured answer breaks the contract or declares inability; findings are leads |
| `failed` | Native/startup/process error or no completed final answer |
| `skipped` | Admission refused before review inference; report cause and remedy |
| `timeout` | An explicit limit stopped the lane; drafts are diagnostic |
| `interrupted` | Runner was signalled or explicitly cancelled |

Report every non-done lane and all policy/MCP diagnostics, including diagnostics from done lanes. Exit 0 from `run` means settlement, not universal success. Exit 2 is pre-spend refusal; wait exit 3 means still pending; 130 means the supervisor was interrupted.

Check each finding's premises against the actual source before evaluating the argument. Reject as wrong only with a contradictory observation; otherwise retain uncertainty. Judge repairs separately from diagnoses. Prefer a regression that fails while the defect is present. Do not count votes or treat unavailable lanes as disagreement.

Write `CONSENSUS.md` with one decision line; confirmed/rejected/uncertain findings with lane provenance; selected repairs and their verification; rejected repairs and reasons; coverage gaps. [The example](../examples/CONSENSUS.example.md) is illustrative, not a recorded live review. Report uncertain rows to the operator. Another paid/subscription round is justified only by useful evidence still missing, not by a desire to ratify a deterministic repair.

## What is included

The runner, native/ACP adapters, public protocol, examples and offline tests are included. A scheduler for free-model qualification, host-specific completion callbacks, Herdr caller verification, native credential provisioning and OS sandbox wrappers are separate integration responsibilities. Default pins are dated choices, not availability guarantees. Recheck provider prices and data terms before selecting free offers; do not replace a subscription route with a paid API route.
