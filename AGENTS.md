# xcheck — public source

Read `docs/publication.md` before editing or publishing. Read the full protocol
and the affected integration contract before changing runtime behavior.

- Public: reusable review method, code, truthful constraints, synthetic examples
  and sanitized regression evidence.
- Private: deployment topology, host/service identities, schedules, account
  inventory, personal instructions, monitoring destinations and live run records.
  Preserve these outside tracked source; never redact them by discarding the
  operator's only copy.
- If `local/operator-workflow.md` exists, read it for this installation's context.
  It is private input, never material to copy into public docs, examples, commit
  messages, issues or review packets.
- Keep credentials in native/secret stores, not notes or local configuration.
- Review the complete index and every outgoing commit, not only the final diff.
  Run `python3 tools/check_publication.py staged` and the tests in
  `tools/tests/`. Private checks require the Git-local policy described in the
  publication guide. A passing check is not proof that prose is safe.
- Stage explicit files; push explicit reviewed refs. Never push all refs, mirror
  a private repository, force-add private files, or bypass a publication guard.
  History rewriting, branch deletion and changes of repository visibility need
  a separate explicit decision.
- Preserve concurrent work. Keep changes scoped; do not modify runtime behavior
  just to remove a deployment anecdote from documentation.
- After changing `bin/` or `xcheck/`, run the full suite, reinstall from the
  intended checkout and run `xcheck doctor`. Changes to transport or permission
  contracts also require the protocol's live qualification.
