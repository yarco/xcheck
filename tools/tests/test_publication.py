"""Synthetic Git repositories only; no native accounts, network or model calls."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

CHECKER = Path(__file__).resolve().parents[1] / "check_publication.py"
ZERO = "0" * 40
DESTINATION = "https://example.test/public.git"


class Publication(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.root = Path(self.scratch.name)
        self.repo = self.root / "source"
        self.repo.mkdir()
        self.home = self.root / "home"
        self.home.mkdir()
        self.env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(self.home),
            "TMPDIR": str(self.root), "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull, "LC_ALL": "C",
        }
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "Example Author")
        self.git("config", "user.email", "author@example.test")
        self.write("README.md", "Portable documentation.\n")
        self.git("add", "README.md")
        self.git("commit", "-qm", "Initial public example")
        self.base = self.oid()
        # Intentionally not recognizable by a generic secret pattern.
        self.marker = "private" + "-deployment-" + "sentinel"
        self.policy = {
            "private_terms": [self.marker], "public_bases": [],
            "allowed_refs": ["refs/heads/main"],
            "allowed_remotes": [DESTINATION],
        }
        self.save_policy()

    def run_command(self, args, *, input=None, env=None):
        return subprocess.run(args, cwd=self.repo, env=env or self.env,
                              input=input, text=True, capture_output=True)

    def git(self, *args):
        result = self.run_command(["git", *args])
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def oid(self):
        return self.git("rev-parse", "HEAD")

    def write(self, name, content):
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    def save_policy(self):
        self.write(".git/publication-policy.json", json.dumps(self.policy))

    def approve_rewrite(self, updates=None, **overrides):
        plan = {
            "remote": DESTINATION, "expires_at": int(time.time()) + 600,
            "updates": updates or [
                {"ref": "refs/heads/main", "old": self.base, "new": self.oid()}],
        }
        plan.update(overrides)
        self.write(".git/publication-rewrite.json", json.dumps(plan))
        (self.repo / ".git/publication-rewrite.json").chmod(0o600)

    def new_root(self):
        self.git("checkout", "--orphan", "replacement", "-q")
        self.git("commit", "-qm", "Reviewed public snapshot")
        return self.oid()

    def check(self, *args, input=None, env=None):
        return self.run_command([sys.executable, "-I", str(CHECKER), *args],
                                input=input, env=env)

    def push(self, *, tip=None, old=None, local="refs/heads/main",
             remote="refs/heads/main", destination=DESTINATION):
        return self.check("push", "origin", destination,
                          input=f"{local} {tip or self.oid()} {remote} {old or self.base}\n")

    def assertBlocked(self, result, reason):
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(reason, result.stderr)
        self.assertNotIn(self.marker, result.stdout + result.stderr)

    def test_clean_index_and_revision(self):
        self.assertEqual(self.check("staged").returncode, 0)
        self.assertEqual(self.check("revision", "HEAD").returncode, 0)
        self.assertEqual(self.push().returncode, 0)

    def test_partial_staging_scans_index_not_repaired_worktree(self):
        self.write("README.md", self.marker)
        self.git("add", "README.md")
        self.write("README.md", "Already repaired on disk, not staged.")
        self.assertBlocked(self.check("staged"), "private marker")

    def test_unstaged_private_content_does_not_publish(self):
        self.write("README.md", self.marker)
        self.assertEqual(self.check("staged").returncode, 0)

    def test_force_added_ignored_local_file_is_blocked(self):
        self.write(".gitignore", "local/\n")
        self.git("add", ".gitignore")
        self.write("local/notes.md", "Unremarkable text; the path alone is private.")
        self.git("add", "-f", "local/notes.md")
        self.assertBlocked(self.check("staged"), "private/artifact path")

    def test_private_filename_is_not_echoed(self):
        self.write(self.marker + "\nname.md", "ordinary content")
        self.git("add", "--", self.marker + "\nname.md")
        self.assertBlocked(self.check("staged"), "filename: private marker")

    def test_intermediate_leak_is_blocked_even_with_clean_tip(self):
        self.write("README.md", self.marker)
        self.git("add", "README.md")
        self.git("commit", "-qm", "Intermediate change")
        self.write("README.md", "Clean final tree")
        self.git("add", "README.md")
        self.git("commit", "-qm", "Remove intermediate content")
        self.assertEqual(self.check("revision", "HEAD").returncode, 0)
        self.assertBlocked(self.push(), "private marker")

    def test_commit_message_is_checked(self):
        self.git("commit", "--allow-empty", "-qm", self.marker)
        self.assertBlocked(self.push(), "commit metadata")

    def test_new_branch_scans_ancestry_not_local_remote_tracking_guess(self):
        self.git("commit", "--allow-empty", "-qm", self.marker)
        self.git("commit", "--allow-empty", "-qm", "Clean tip")
        self.git("update-ref", "refs/remotes/origin/unverified", self.oid())
        self.assertBlocked(self.push(old=ZERO), "private marker")

    def test_explicit_public_base_excludes_old_history_but_not_tip(self):
        self.git("commit", "--allow-empty", "-qm", self.marker)
        self.policy["public_bases"] = [self.oid()]
        self.save_policy()
        self.git("commit", "--allow-empty", "-qm", "New safe metadata")
        self.assertEqual(self.push(old=ZERO).returncode, 0)
        self.write("README.md", self.marker)
        self.git("add", "README.md")
        self.git("commit", "-qm", "New file content")
        self.policy["public_bases"] = [self.oid()]
        self.save_policy()
        self.assertBlocked(self.push(old=ZERO), "private marker")

    def test_all_updates_are_checked(self):
        self.git("commit", "--allow-empty", "-qm", self.marker)
        rows = (f"refs/heads/main {self.base} refs/heads/main {self.base}\n"
                f"refs/heads/main {self.oid()} refs/heads/main {self.base}\n")
        self.assertBlocked(self.check("push", "origin", DESTINATION, input=rows),
                           "private marker")

    def test_private_source_and_unapproved_destination_refs(self):
        self.assertBlocked(self.push(local="refs/heads/private/backup"), "private source ref")
        self.assertBlocked(self.push(remote="refs/heads/unreviewed"), "destination ref")
        self.assertBlocked(self.push(remote="refs/tags/release"), "destination ref")
        self.assertBlocked(self.push(destination="https://example.test/other.git"),
                           "destination is not approved")

    def test_ref_names_are_scanned(self):
        self.assertBlocked(self.push(local="refs/heads/" + self.marker), "source ref")

    def test_deletion_and_non_fast_forward_are_refused(self):
        self.assertBlocked(self.push(tip=ZERO), "ref deletion")
        self.git("commit", "--allow-empty", "-qm", "Remote successor")
        self.assertBlocked(self.push(tip=self.base, old=self.oid()), "non-fast-forward")

    def test_exact_rewrite_scans_root_and_does_not_consume_approval(self):
        self.new_root()
        self.approve_rewrite()
        self.assertEqual(self.push().returncode, 0)
        # The hook cannot know if the server accepted the push. The caller must
        # verify the result, then remove the approval (including after failure).
        self.assertTrue((self.repo / ".git/publication-rewrite.json").exists())
        self.assertEqual(self.push().returncode, 0)

    def test_rewrite_checks_all_history_despite_public_bases(self):
        self.new_root()
        self.write("README.md", self.marker)
        self.git("add", "README.md")
        self.git("commit", "-qm", "Intermediate content")
        leaked = self.oid()
        self.write("README.md", "Clean tip")
        self.git("add", "README.md")
        self.git("commit", "-qm", "Clean tip")
        self.policy["public_bases"] = [leaked, self.oid()]
        self.save_policy()
        self.approve_rewrite()
        self.assertBlocked(self.push(), "private marker")

    def test_rewrite_checks_commit_metadata(self):
        self.new_root()
        self.git("commit", "--allow-empty", "-qm", self.marker)
        self.approve_rewrite()
        self.assertBlocked(self.push(), "commit metadata")

    def test_rewrite_requires_exact_destination_old_and_new(self):
        tip = self.new_root()
        self.approve_rewrite()
        self.assertBlocked(self.push(old=ZERO), "rewrite approval")
        self.assertBlocked(self.push(old=tip), "rewrite approval")
        self.git("commit", "--allow-empty", "-qm", "Unapproved successor")
        self.assertBlocked(self.push(), "rewrite approval")
        self.assertBlocked(self.push(tip=tip, local="refs/heads/private/source"),
                           "private source ref")
        self.assertBlocked(self.push(tip=tip, remote="refs/heads/other"), "destination ref")
        self.policy["allowed_remotes"].append("https://example.test/other.git")
        self.save_policy()
        self.assertBlocked(self.push(tip=tip, destination=self.policy["allowed_remotes"][-1]),
                           "rewrite approval")

    def test_rewrite_requires_complete_unique_update_set(self):
        tip = self.new_root()
        self.policy["allowed_refs"].append("refs/heads/alternate")
        self.save_policy()
        updates = [{"ref": ref, "old": self.base, "new": tip}
                   for ref in self.policy["allowed_refs"]]
        self.approve_rewrite(updates)
        rows = [f"HEAD {tip} {u['ref']} {self.base}\n" for u in updates]
        self.assertEqual(self.check("push", "origin", DESTINATION,
                                    input="".join(reversed(rows))).returncode, 0)
        for subset in (rows[:1], rows + rows[:1], [rows[0], rows[0]]):
            self.assertBlocked(self.check("push", "origin", DESTINATION,
                                          input="".join(subset)), "rewrite approval")
        self.approve_rewrite([updates[0], updates[0]])
        self.assertBlocked(self.check("push", "origin", DESTINATION, input="".join(rows)),
                           "rewrite approval")

    def test_invalid_expired_or_broad_rewrite_approval_fails_closed(self):
        self.new_root()
        cases = [
            {"expires_at": int(time.time()) - 1},
            {"expires_at": int(time.time()) + 7200},
            {"expires_at": True},
            {"updates": []},
            {"updates": [{}]},
            {"updates": [{"ref": "refs/heads/main", "old": ZERO, "new": self.oid()}]},
            {"updates": [{"ref": "refs/heads/main", "old": self.base, "new": "HEAD"}]},
            {"unexpected": True},
        ]
        for changes in cases:
            with self.subTest(changes=changes):
                self.approve_rewrite()
                path = self.repo / ".git/publication-rewrite.json"
                plan = json.loads(path.read_text())
                plan.update(changes)
                path.write_text(json.dumps(plan))
                self.assertBlocked(self.push(), "rewrite approval")

    def test_unsafe_approval_file_is_refused(self):
        self.new_root()
        self.approve_rewrite()
        path = self.repo / ".git/publication-rewrite.json"
        path.chmod(0o644)
        self.assertBlocked(self.push(), "rewrite approval")
        path.chmod(0o600)
        for data in ("{malformed", "x" * 65537):
            path.write_text(data)
            self.assertBlocked(self.push(), "rewrite approval")
        path.unlink()
        path.symlink_to(self.root / "missing")
        self.assertBlocked(self.push(), "rewrite approval")
        path.unlink()
        os.mkfifo(path, 0o600)
        self.assertBlocked(self.push(), "rewrite approval")

    def test_rewrite_does_not_authorize_deletion_or_weak_history(self):
        self.new_root()
        self.approve_rewrite()
        self.assertBlocked(self.push(tip=ZERO), "ref deletion")
        self.write(".git/shallow", self.oid() + "\n")
        self.assertBlocked(self.push(), "shallow")
        (self.repo / ".git/shallow").unlink()
        self.git("config", "remote.origin.promisor", "true")
        self.assertBlocked(self.push(), "complete local clone")

    def test_real_guarded_rewrite_with_lease_and_revocation(self):
        remote = self.root / "destination.git"
        self.git("init", "--bare", "-q", str(remote))
        self.policy["allowed_remotes"] = [str(remote)]
        self.save_policy()
        self.assertEqual(self.check("install-hooks").returncode, 0)
        self.git("remote", "add", "origin", str(remote))
        self.git("push", "-q", "origin", "HEAD:refs/heads/main")
        tip = self.new_root()
        args = ["push", "--atomic", f"--force-with-lease=refs/heads/main:{self.base}",
                "origin", "HEAD:refs/heads/main"]
        self.assertBlocked(self.run_command(["git", *args]), "non-fast-forward")
        self.approve_rewrite(remote=str(remote))
        self.git(*args)
        self.assertEqual(self.git("--git-dir=" + str(remote), "rev-parse", "refs/heads/main"),
                         tip)
        (self.repo / ".git/publication-rewrite.json").unlink()
        self.assertBlocked(self.push(tip=self.base, old=tip, destination=str(remote)),
                           "non-fast-forward")

    def test_atomic_rewrite_does_not_overwrite_concurrent_remote_work(self):
        remote = self.root / "destination.git"
        self.git("init", "--bare", "-q", str(remote))
        refs = ["refs/heads/main", "refs/heads/alternate"]
        self.policy.update(allowed_remotes=[str(remote)], allowed_refs=refs)
        self.save_policy()
        self.assertEqual(self.check("install-hooks").returncode, 0)
        self.git("remote", "add", "origin", str(remote))
        self.git("push", "-q", "origin", *["HEAD:" + ref for ref in refs])
        tip = self.new_root()
        self.approve_rewrite(
            [{"ref": ref, "old": self.base, "new": tip} for ref in refs],
            remote=str(remote))
        # A separate actor advances one branch after the approval was prepared.
        self.git("--git-dir=" + str(remote), "config", "user.name", "Example Author")
        self.git("--git-dir=" + str(remote), "config", "user.email", "author@example.test")
        result = self.run_command(
            ["git", "--git-dir=" + str(remote), "commit-tree",
             self.git("rev-parse", self.base + "^{tree}"), "-p", self.base],
            input="Concurrent remote update\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        concurrent = result.stdout.strip()
        self.git("--git-dir=" + str(remote), "update-ref", refs[1], concurrent, self.base)
        result = self.run_command(
            ["git", "push", "--atomic",
             *[f"--force-with-lease={ref}:{self.base}" for ref in refs],
             "origin", *["HEAD:" + ref for ref in refs]])
        self.assertNotEqual(result.returncode, 0)
        for ref, expected in zip(refs, [self.base, concurrent]):
            self.assertEqual(self.git("--git-dir=" + str(remote), "rev-parse", ref),
                             expected)

    def test_missing_policy_is_optional_only_for_standalone_checks(self):
        (self.repo / ".git/publication-policy.json").unlink()
        self.assertEqual(self.check("staged").returncode, 0)
        self.assertBlocked(self.check("--require-policy", "staged"), "policy is missing")
        self.assertBlocked(self.push(), "policy is missing")

    def test_invalid_policy_and_missing_objects_fail_closed(self):
        self.write(".git/publication-policy.json", "{malformed")
        self.assertBlocked(self.check("staged"), "invalid Git-local")
        self.save_policy()
        self.assertBlocked(self.push(old="f" * 40), "Git read failed")
        self.assertBlocked(self.check("push", "origin", DESTINATION, input="bad\n"), "malformed")
        self.assertBlocked(self.check("revision", "--not-a-ref"), "error")

    def test_binary_oversized_and_submodule_entries_refused(self):
        (self.repo / "image.dat").write_bytes(b"\x00data")
        self.git("add", "image.dat")
        self.assertBlocked(self.check("staged"), "non-text object")
        self.git("reset", "-q", "HEAD", "--", "image.dat")
        self.write("large.txt", "x" * (2 * 1024 * 1024 + 1))
        self.git("add", "large.txt")
        self.assertBlocked(self.check("staged"), "oversized blob")
        self.git("reset", "-q", "HEAD", "--", "large.txt")
        self.git("update-index", "--add", "--cacheinfo", f"160000,{self.base},dependency")
        self.assertBlocked(self.check("staged"), "unsupported Git entry")

    def test_generic_patterns_without_private_policy(self):
        cases = [
            ("/" + "Users/" + "person/project", "personal-home-path"),
            ("-----" + "BEGIN PRIVATE KEY-----", "private-key"),
            ("https://" + "account:password@" + "example.test/", "credential-url"),
            ("gh" + "p_" + "a" * 36, "provider-token"),
        ]
        for data, reason in cases:
            with self.subTest(reason=reason):
                self.write("README.md", data)
                self.git("add", "README.md")
                result = self.check("staged")
                self.assertBlocked(result, reason)
                self.assertNotIn(data, result.stdout + result.stderr)

    def test_alternate_index_is_honored(self):
        self.write("README.md", self.marker)
        index = str(self.repo / ".git/alternate-index")
        env = dict(self.env, GIT_INDEX_FILE=index)
        result = self.run_command(["git", "read-tree", "HEAD"], env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        result = self.run_command(["git", "add", "README.md"], env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.check("staged").returncode, 0)
        self.assertBlocked(self.check("staged", env=env), "private marker")

    def test_unmerged_index_is_refused(self):
        blob = self.git("rev-parse", "HEAD:README.md")
        self.git("update-index", "--force-remove", "README.md")
        result = self.run_command(
            ["git", "update-index", "--index-info"],
            input=f"100644 {blob} 1\tREADME.md\n100644 {blob} 2\tREADME.md\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertBlocked(self.check("staged"), "unmerged index")

    def test_grafts_shallow_and_partial_clones_refused(self):
        self.write(".git/info/grafts", self.base + "\n")
        self.assertBlocked(self.push(), "grafts")
        (self.repo / ".git/info/grafts").unlink()
        self.write(".git/shallow", self.base + "\n")
        self.assertBlocked(self.push(), "shallow")
        (self.repo / ".git/shallow").unlink()
        self.git("config", "remote.origin.promisor", "true")
        self.assertBlocked(self.push(), "complete local clone")

    def test_replace_refs_cannot_hide_content(self):
        self.write("README.md", self.marker)
        self.git("add", "README.md")
        self.git("commit", "-qm", "New content")
        tip = self.oid()
        self.git("replace", tip, self.base)
        self.assertBlocked(self.push(tip=tip), "private marker")

    def test_hooks_enforce_commit_and_real_local_push(self):
        remote = self.root / "destination.git"
        self.git("init", "--bare", "-q", str(remote))
        self.policy["allowed_remotes"] = [str(remote)]
        self.save_policy()
        self.assertEqual(self.check("install-hooks").returncode, 0)
        self.git("remote", "add", "origin", str(remote))
        self.git("push", "-q", "origin", "HEAD:refs/heads/main")
        self.write("README.md", self.marker)
        self.git("add", "README.md")
        result = self.run_command(["git", "commit", "-qm", "Must not commit"])
        self.assertBlocked(result, "private marker")
        self.assertEqual(self.oid(), self.base)
        # Simulate plumbing bypass of commit hooks. The push hook must still block.
        tree = self.git("write-tree")
        result = self.run_command(["git", "commit-tree", tree, "-p", self.base],
                                  input="Unreviewed change\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        leaked = result.stdout.strip()
        self.git("update-ref", "refs/heads/main", leaked)
        result = self.run_command(["git", "push", "origin", "HEAD:refs/heads/main"])
        self.assertBlocked(result, "private marker")
        self.assertEqual(self.git("--git-dir=" + str(remote), "rev-parse", "refs/heads/main"),
                         self.base)

    def test_hooks_require_policy_and_survive_older_worktrees(self):
        self.assertEqual(self.check("install-hooks").returncode, 0)
        linked = self.root / "linked"
        self.git("worktree", "add", "-q", "--detach", str(linked), self.base)
        self.repo = linked
        result = self.run_command(["git", "commit", "--allow-empty", "-qm", "Worktree change"])
        self.assertEqual(result.returncode, 0, result.stderr)
        (self.root / "source/.git/publication-policy.json").unlink()
        result = self.run_command(["git", "commit", "--allow-empty", "-qm", "Blocked"])
        self.assertBlocked(result, "policy is missing")

    def test_foreign_hooks_and_empty_hooks_path_are_not_overwritten(self):
        hook = self.repo / ".git/hooks/pre-commit"
        hook.write_text("#!/bin/sh\nexit 0\n")
        self.assertBlocked(self.check("install-hooks"), "existing hook")
        self.assertEqual(hook.read_text(), "#!/bin/sh\nexit 0\n")
        hook.unlink()
        self.git("config", "core.hooksPath", "")
        self.assertBlocked(self.check("install-hooks"), "custom hooksPath")


if __name__ == "__main__":
    unittest.main()
