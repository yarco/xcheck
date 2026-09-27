# X-Check v2 — build tracker

## Design cross-check (v1 protocol, Herdr wall) — 2026-09-27, closed

- BASE: `/tmp/agent-offload/xcheck-v2-design-20260927`
- Target: v1 at 596877f2dd57e832dad4b06186599853bf2efe3b + uncommitted.diff (staged working tree)
- Proof target: `evidence/manifest-tail.txt` (MANIFEST_TAIL_TOKEN: 5843c76cda95f9364c487fe6)
- Roster: codex-gpt-6-sol-high, grok-4.7-xhigh, agy-gemini-3.1-pro-high, agy-gemini-3.8-flash-high, agy-claude-sonnet-4-6 (orchestrator claude-opus-5-5; no Claude peer)
- Capacity 17:40Z: codex 65% · grok 87% · agy-gemini 53.9% · agy-3p 60.9% — all eligible, no overrides
- WEB_MODE=live, LIVE_CHECK_CAP 10, DEADLINE 2400
- Command: `WEB_MODE=live /Users/yarco/.agents/xcheck/bin/herdr-host --base /tmp/agent-offload/xcheck-v2-design-20260927 --deadline 2400 --orch "claude"`
- Resume: `WEB_MODE=live /Users/yarco/.agents/xcheck/bin/herdr-host --base /tmp/agent-offload/xcheck-v2-design-20260927 --deadline 2400 --orch "claude" --wait-only`
- Orchestrator plan (peers open it only after their own design): `/tmp/agent-offload/xcheck-v2-design-20260927/evidence/plan/PLAN-claude-opus-5-5.md`

## v2 initial implementation — 2026-09-27

- Done: adapters (4 harnesses, flags verified by `xcheck doctor` + real smoke), lane runner, headless + Herdr placement, validation, capacity (v1 helper minus runtime hook), status, `wait`, tests (25, fake harness + real stream fixtures).
- Real headless smoke: codex/claude/grok/agy all `done` on a 3-line packet.
- Done since: design cross-check returned (5/5 `done`, adjudicated in its BASE `CONSENSUS.md`), findings folded in, Herdr-mode live smoke passed, Agy read reach re-measured, folders swapped (v1 → `../x-check-old/`, runtime re-installed under `~/.agents/xcheck-runtimes/x-check-old/`), vault AGENTS.md repointed.

## Open (operator decisions)

- Web in peers: default `packet` or `live`.
- Herdr wall: rendered event stream (current) or real vendor TUIs.
- Codex orchestrator support in v2 (today: `../x-check-old/`).

## Verification cross-check (v2 on itself) — launched 2026-09-27

- BASE: `/tmp/agent-offload/xcheck-v2-verify-20260927`; target: v2 at b19f5a3; ledgers in `evidence/ledger/` (REQUIREMENTS, DECISIONS D1–D26, GAPS), measurements M1–M13
- Proof: `evidence/manifest-tail.txt` (MANIFEST_TAIL_TOKEN: c0c285107e847f41667f584f)
- Roster: default minus claude (codex-gpt-6-sol-high, grok-4.7-xhigh, agy-gemini-3.1-pro-high, agy-gemini-3.8-flash-high, agy-claude-sonnet-4-6); web live; capacity per lane
- Command: `xcheck run --base /tmp/agent-offload/xcheck-v2-verify-20260927 --orch claude` (background job); resume: `xcheck wait --base /tmp/agent-offload/xcheck-v2-verify-20260927`
