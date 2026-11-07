# Public source, private operations

xcheck shares a review method and its implementation, not a maintainer's working
environment. Document what an adopter needs to understand, install, verify and
extend the product. Describe interfaces and failure modes without narrating a
particular deployment.

## Ownership

| Material | Home |
|---|---|
| Protocol, supported behavior, limitations, code and synthetic examples | Tracked public source |
| Sanitized regression captures, vendor versions and reproducible qualification method | Tracked public tests/integration docs |
| Host topology, service/account names, schedules, monitoring, account inventory, personal rules and live qualification results | Private runbooks outside tracked source; ignored `local/` may hold a small installation overlay |
| Actual packet, prompts, outputs, native logs and diagnostics | Private run directory, never an issue attachment by default |
| Tokens, keys, native login files and refresh credentials | Native credential or secret store, never documentation |

An installation overlay should reference the public contract, not duplicate it.
When moving operational detail out of source, preserve the original privately
with source revision and date first. Keep backups outside the public checkout.
Git ignore rules do not control cloud sync, backups or filesystem permissions,
and they do not untrack a file already in Git.

Portable facts stay public: native flags, model-selection constraints, generic
paths used by the runtime, credential-custody interfaces and limitations such as
unrestricted shell access. Do not hide a safety limitation to make a release look
cleaner. Separate a vendor observation from the operator's accounts or tool
inventory. Use synthetic identities and data while preserving parser structure.

## Before every publication

1. Review the purpose of every changed file. Remove private facts from the public
   version, not just recognized secrets. Inspect links, examples, comments,
   fixtures, filenames and commit metadata too.
2. Stage only explicit paths. Inspect `git diff --cached --stat`,
   `git diff --cached` and `git ls-files --stage`. A worktree edit does not fix a
   different version already in the index.
3. Run the guard and its regression suite:

   ```bash
   python3 tools/check_publication.py staged
   python3 -m unittest discover -s tools/tests -v
   git diff --cached --check
   ```

4. Review every outgoing commit and intended destination. A sensitive file added
   and removed in successive commits is still published. Check non-default
   branches, tags, releases and attachments when they are in scope, not just
   `main`. Never use `push --all`, `--mirror` or blanket `--tags`.
5. Push an explicit reviewed ref, for example
   `git push origin HEAD:refs/heads/main`. Verify the remote head and public
   files, and verify local material remains untracked.

Issues, PR descriptions, screenshots, release archives and tool output are
publication surfaces too. The Git guard cannot inspect them.

## Local guard

`tools/check_publication.py` uses only Python's standard library and local Git
objects. It checks the complete index or a requested revision for private/run
artifact paths, personal home paths, a few common credential forms and optional
private markers. It refuses binary, oversized and unsupported entries instead
of silently skipping them. This text-only repository currently needs none of
those entries; adding them needs an explicitly reviewed extension to the guard.

Private marker vocabulary must not be published as a public denylist. Create
`publication-policy.json` under the directory returned by
`git rev-parse --git-common-dir`, with this shape:

```json
{
  "private_terms": [],
  "public_bases": [],
  "allowed_refs": ["refs/heads/main"],
  "allowed_remotes": ["https://github.com/OWNER/REPOSITORY.git"]
}
```

Fill `private_terms` locally with installation-specific phrases, internal
identifiers and private paths (literal case-insensitive strings, at least three
characters). Do not put credentials in the policy. Set `allowed_remotes` to exact
approved push URLs and list only intended public branch refs. Protect the policy
with owner-only permissions. Missing optional policy in a standalone staged or
revision scan means **generic checks only**; installed hooks require it and fail
closed if it is missing or invalid.

`public_bases` accepts full commit IDs whose ancestry is **already published**
at the approved destination, independently verified by the maintainer. Start
empty for a new repository. On an existing repository, audit its history before
recording such anchors. They distinguish old exposure from newly outgoing
commits; they do not certify old history as clean. Never add a private commit as
a base merely to silence a finding.

After reviewing the checker and policy:

```bash
python3 tools/check_publication.py install-hooks
```

This installs a reviewed checker snapshot and `pre-commit`/`pre-push` hooks in
the common Git directory. It does not install xcheck or alter native accounts.
It refuses custom hook directories or unrelated existing hooks rather than
overwriting them. The snapshot also guards older branches/worktrees that lack
the checker; deliberately reinstall it after reviewing checker changes.

The commit hook scans staged blobs. The push hook checks every ref update
against the approved destination and branch list, refuses private source refs,
deletions and unapproved non-fast-forward updates, and scans each newly reachable
commit's metadata and full tree. The remote ref's old ancestry and explicit public bases
are excluded from the new-history scan; **the final tree is always scanned**.
Thus an intermediate leak still blocks a push even if the tip removed it.
Shallow or grafted history and partial clones are refused. Missing objects or
policy/read failures block rather than trigger implicit network retrieval.

Findings identify object hashes and entry numbers, never matching values or
filenames. Inspect those objects privately. Hooks are local, not shipped active
to new clones and not a server-side guarantee. They can be bypassed by Git
plumbing or disabled hooks; other clones must install their own. Pattern checks
cannot recognize all secrets, operational prose, encodings or new host details.
Human review remains required.

The `Publication guard` CI job runs these synthetic Git tests and the offline
installation-to-results demo in an unprivileged service with no external network,
an empty allowlisted environment, read-only source, bounded scratch storage and
CPU/memory/process/time limits. It exports only the required tracked source and
uses the runner's existing Python and Git. It never loads local publication
policy, accounts, deployment configuration or real model lanes. Passing CI
validates the guard's tested behavior and fake-lane collection, not the privacy
of a contributor's prose, private marker coverage or live adapter compatibility.

## Authorized history replacement

Rewriting published history requires the owner's explicit approval and a verified
private recovery bundle. Prefer reviewed clean-root snapshots when reliably
sanitizing every historical tree and commit message is impractical. Preserve
each supported branch's current contract; do not merge obsolete implementations
into the default branch just to give them shared ancestry.

The guard supports an exact, short-lived approval in `publication-rewrite.json`
in the common Git directory. This local file must be a regular, owner-only file
(mode `0600`), never tracked or shared. It has these fields:

- `remote`: the exact destination URL, also approved in the ordinary policy.
- `expires_at`: integer Unix seconds, in the future and no more than one hour away.
- `updates`: the complete list of `{ "ref": ..., "old": ..., "new": ... } entries.
  Each ref is an approved branch; old and new are distinct, nonzero full commit
  IDs. Wildcards, missing refs, extra refs, duplicates and deletions are refused.

Review the complete replacement histories and create this approval only after
their final commit IDs are known. With an approval present, the push must match
it exactly, and **every replacement ancestor, tree and commit message is scanned
without public-base exemptions**. All ordinary destination and source-ref checks
still apply. A stale or invalid approval blocks publication rather than falling
back to ordinary push behavior.

Push all approved refs together with `--atomic` and an explicit
`--force-with-lease=refs/heads/BRANCH:OLD_ID` for each branch. Never use bare
`--force`, disable hooks or republish a recovery bundle. The guard checks the
remote old IDs reported by Git; the caller must supply leases and atomicity.
After any attempt, verify the actual remote refs and remove the approval,
including on failure. The pre-push hook cannot know the server outcome and does
not consume approvals itself. A retry needs a fresh review of remote state.

After success, remove obsolete `public_bases` from the local policy, verify a
fresh clone's complete history, and align only the intended local branches.
Keep recovery material and unrelated worktrees private. Consumers should save
their local work and re-clone; merging the old history back can restore exposure.
Do not attach old diffs to public issues. Check retained hosting artifacts and
request host-side cleanup where necessary; a ref rewrite is not cache erasure.

## If something was already published

Stop adding exposure and preserve an owner-private backup. Revoke or rotate any
exposed credential; deleting a file is not revocation. Identify affected public
refs, commits, releases, attachments and mirrors. Make the current content safe,
but report residual history honestly.

History rewriting, ref deletion and repository visibility changes are separate
decisions because they disrupt consumers and may not remove cached or copied
content. An ordinary cleanup commit does not erase old blobs. Even a coordinated
rewrite cannot recall existing clones, forks, archives or third-party caches.
