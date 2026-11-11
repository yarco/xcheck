# Runtime ownership and change contracts

The [protocol](../Multi-Agent%20Cross-Check%20Protocol.md) defines review behavior.
This document defines the boundaries used to maintain that behavior. Simplifying
a mechanism must preserve its requirements and the evidence that motivated them.
Line count is not an acceptance criterion.

## Execution and authority

```text
CLI → supervisor → lane runner → native harness / acpx → native ACP server
                      │                    │
                      │                    └─ raw stream → Reader
                      │                                      │
                      └─ Outcome + Reader + packet contract → settlement
                                                               │
                                                    RESULT, then exit.json
```

Herdr changes where the runner executes, not this ownership chain. The runner
holds the lane life lock, one concurrency permit and aggregate pool custody.
Its harness inherits both custody descriptors; `xcheck hold` rejoins that exact
lane's permit across acpx's stdio-only child boundary. The process group is
settled before the runner releases its permit. An independently
owned authentication watcher retains its own locks and parent-EOF lease because
native sign-in must also stop when the runner dies.

| Module | Responsibility | Must not decide |
|---|---|---|
| `types.py` | Lane identity, pool identity, launch value | Availability, liveness, review validity |
| `environment.py` | Scrub inherited control/routing; apply deliberate launch environment | Account recovery or process lifetime |
| `adapters.py`, `cline.py` | Explicit native launch policy, roster, stream semantics | Supervisor lifecycle or review merit |
| `grok.py`, `agy.py` | Vendor-specific, inference-free admission observations | Generic lane termination |
| `models.py` | Provider-owned price/access observations and client metadata | Reviewer quality or stale-price authorization |
| `capacity.py`, `claude_auth.py` | Quota admission and owned pre-inference authentication | Retrying a reviewer turn |
| `lane.py` | Stage after pool admission, own child, read stream, enforce requested cancellation policy | Pane text as completion |
| `settlement.py` | Outcome precedence and the single terminal publication implementation | Killing processes or changing native evidence |
| `run.py` | Snapshot, place, supervise, recover, cancel; publish summary | Native protocol interpretation |
| `doctor.py` | Report readiness using the same native checks as admission | Repairing credentials or launching inference |
| `view.py`, `activity.py` | Transcript and progress diagnostics | Success, cancellation or retry |

Vendor differences remain explicit. A shared execution mechanism does not imply
that native permission flags, credentials, model identity or retry behavior are
interchangeable. There is no plugin framework or custom replacement ACP client.

## One settlement policy

`Outcome` carries the process result and any explicit termination decision.
Normal completion, runner exceptions and dead-runner replay use the same policy:

1. Preserve an existing `exit.json`.
2. A recorded timeout, interruption, admission refusal or native terminal failure
   wins over a complete-looking answer.
3. Otherwise a native error fails the lane.
4. Without native completion, output remains a diagnostic draft.
5. Native completion needs final text; validate that text against the immutable
   packet contract to obtain `done` or `invalid`.

An incidental runner failure can recover a genuinely completed answer. It cannot
erase a prior termination decision. The runner persists that decision in
`termination.json` before cleanup, so abrupt death cannot make stream replay
forget it. This is recovery input, not a second completion signal. The reader's
raw stream remains unchanged. `cancel.json` also retains an explicit operator
request before the runner can acknowledge it.

The lane life lock serializes runner, recovery and cancellation publication.
The initial runner waits out any observer/recovery custody and then checks for
an existing terminal record; observers cannot make its initial lock acquisition
fail. Recovery and cancellation remain nonblocking and report pending custody
honestly. Exception recovery defers further stop signals through publication
and credential cleanup. Every publication path consults durable stop records
when it has no earlier in-memory decision, including cancellation requests that
arrive before their signal is delivered.
Recovery acquires it, checks `exit.json` again, verifies any orphan's identity,
settles the owned process group where possible, and only then publishes. A
recorded PID alone never grants kill authority. Unverifiable orphan identity
remains an explicit limitation; inherited custody keeps its permit occupied.

Result bytes are written before `exit.json`; terminal records are immutable.
Failure of the storage itself can still prevent publication. Reattachment can
reconstruct a result once storage is usable; no system can promise a durable
record while its filesystem refuses writes.

## Observers and summary ownership

`status` and completed `wait` derive their output from lane records. They do not
write shared summaries. Only the supervisor holding `supervisor.alive` publishes
`STATUS.json` and `STATUS.md`, then `.done` when every lane has settled.

The two summary files are convenient projections, not an atomic multi-file
transaction. `.done` is written after both final projections. A concurrent
observer cannot overwrite them with an earlier view. A completed waiter reads
the authoritative records even if an old or external writer left stale summary
files. A bounded pending wait renders current state without taking publication
ownership.

Display and progress are best effort. A display exception disables that display
and records a diagnostic; the review continues. Authentication progress and
header formatting use the same guarded display. A failure first occurring in
the final post-publication display cannot be added to immutable `exit.json`;
publication remains ahead of presentation. Process cleanup is in an
unconditional nested `finally`, even if final transcript flushing fails. The
Reader computes native state only; the old discarded display-return interface
is removed. Raw stream parsing, transcript rendering and text-free activity have
different jobs and do not certify one another.

## Known failure versus uncertainty

A synchronous headless spawn refusal proves no child was created: record that
lane as failed and attempt independent peers. A Herdr delivery failure may occur
after the command was submitted: retain uncertainty until a runner appears or
the operator cancels. Silence and elapsed time cannot convert that uncertainty
into failure unless the operator requested a cancellation limit.

## Requirements preserved through consolidation

| Requirement / motivating failure | Current mechanism | Regression evidence | Retirement condition |
|---|---|---|---|
| A draft can look complete before tools finish | Native Reader completion + final-message selection | `test_runtime_e2e.py`, native captured fixtures | Only an equally authoritative native publication channel |
| Timeout and salvage must not contradict | Shared settlement precedence + durable termination decision | `test_settlement.py`, `CapBeforeCompletion` | A replacement preserves precedence across both exceptions and process death |
| Renderer failures must not affect spending | Best-effort Display + unconditional cleanup | `test_settlement.py` display failure; spawn/setup tests | Diagnostics remain outside execution decisions |
| Concurrent observers can publish stale state | Supervisor-only summary publication; read-only observers | `test_settlement.py` status tests | A stronger single-authority projection mechanism, not more writers |
| Failed spawn is different from uncertain delivery | Per-lane headless failure settlement; explicit Herdr delivery state | `test_settlement.py`, Herdr ownership tests | Native placement supplies an equally precise result |
| Runner death must not release a live spender's permit or allow account rotation | Inherited slot + aggregate flocks and exact-lane ACP hold wrapper | `test_pools.py`, native custody and orphan runtime tests | Verified native descriptor custody across every launch layer |
| Unrelated reviews must overlap without overwriting evidence | Bounded per-lane permits; run-scoped workspaces recorded in `run.json` | `test_pools.py`, same-model two-run overlap / third-run queue regression | Equivalent bounded concurrency and disjoint ownership |
| Queued lanes must see the launched packet | Launch snapshot; run/lane-owned staging | `test_final_repairs.py` queued snapshot regression | Another immutable shared input contract |
| Authentication recovery must survive caller death safely | Owned watchdog, pool/auth locks, EOF lease | `test_claude_auth.py` | Native ownership provides the same guarantee |
| Unknown inventory/price is not absence or free access | Explicit provider observations and admission checks | `test_models.py`, `test_agy_acp.py`, `test_cline.py` | A provider-owned replacement with the same distinctions |
| Grok renamed tool IDs fail its allowlist open | Zero-turn resolved-toolset contract | `test_runner_issues.py` Grok probes | Native allowlists fail closed and their result remains observable |
| Agy's server rewrites Keychain ACLs | Canonical read, isolated 0600 file credential, cleanup | `test_agy_acp.py`; protocol Agy facts | Verified native Keychain custody no longer causes prompts |

For changes, identify the affected rows and native fixtures first. Preserve the
requirements even when deleting the mechanisms. Run focused failing-before
regressions, then the full suite. A changed transport or permission contract also
needs the protocol's live qualification in both placements; fixture success is
not proof of current vendor behavior. Do not repeat a model review merely to
ratify a deterministic repair.
