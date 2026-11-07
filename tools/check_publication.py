#!/usr/bin/env python3
"""Offline publication guard. Read Git objects, never unstaged file contents.

This is a regression aid, not a secret detector or an isolation boundary.
Private deployment vocabulary belongs in Git-local policy, not this source.
"""
import argparse
import json
import os
from pathlib import Path
import re
import shlex
import stat
import subprocess
import sys
import tempfile
import time

MAX_BYTES = 2 * 1024 * 1024
MAX_COMMITS = 10000
HOOK_MARKER = "# xcheck-publication-hook-v1"
PRIVATE_PARTS = {
    "local", "private", ".private", ".ssh", ".packet", ".lanes", ".pools",
    "transcripts", "__pycache__", ".venv",
}
PRIVATE_NAMES = {
    "auth.json", "credentials.json", "publication-policy.json", "publication-rewrite.json",
    "run.json", "status.json", "status.md", "stream.jsonl", "operator-workflow.md",
}
PRIVATE_SUFFIXES = {".bundle", ".db", ".sqlite", ".sqlite3", ".key", ".pem",
                    ".p12", ".pfx", ".log"}
PATTERNS = {
    "personal-home-path": re.compile(r"/(?:Users|home)/[a-zA-Z0-9_.-]+/"),
    "private-key": re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----"),
    "credential-url": re.compile(r"https?://[^/\s:@]+:[^/\s@]+@"),
    "provider-token": re.compile(
        r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,}"
        r"|AKIA[A-Z0-9]{16}|sk-(?:proj-|ant-)?[A-Za-z0-9_-]{32,})\b"),
}


class Refused(Exception):
    pass


def git(*args, ok=(0,)):
    env = dict(os.environ, GIT_NO_REPLACE_OBJECTS="1", GIT_NO_LAZY_FETCH="1")
    result = subprocess.run(["git", *args], env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, check=False)
    if result.returncode not in ok:
        # stderr may contain a secret-bearing filename, URL or ref.
        raise Refused("Git read failed; verify the repository and required objects locally")
    return result.stdout


def common_dir():
    return Path(os.fsdecode(git("rev-parse", "--git-common-dir")).strip()).resolve()


def commit(ref):
    return git("rev-parse", "--verify", "--end-of-options", ref + "^{commit}").decode().strip()


def load_policy(required=False):
    path = common_dir() / "publication-policy.json"
    if not path.exists():
        if required:
            raise Refused("required Git-local publication policy is missing")
        return {"private_terms": [], "public_bases": [], "allowed_refs": [], "allowed_remotes": []}
    try:
        if path.is_symlink() or path.stat().st_size > 65536:
            raise ValueError()
        policy = json.loads(path.read_text())
        keys = {"private_terms", "public_bases", "allowed_refs", "allowed_remotes"}
        if not isinstance(policy, dict) or set(policy) != keys:
            raise ValueError()
        for values in policy.values():
            if not isinstance(values, list) or any(not isinstance(v, str) or not v.strip()
                                                   for v in values):
                raise ValueError()
        if any(len(v) < 3 for v in policy["private_terms"]):
            raise ValueError()
        for oid in policy["public_bases"]:
            if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", oid) or commit(oid) != oid:
                raise ValueError()
        if any(not v.startswith("refs/heads/") for v in policy["allowed_refs"]):
            raise ValueError()
    except (OSError, ValueError, UnicodeError):
        raise Refused("invalid Git-local publication policy; values are not displayed") from None
    return policy


class Scan:
    def __init__(self, policy):
        self.terms = [term.casefold() for term in policy["private_terms"]]
        self.seen = set()
        self.issues = []

    def text(self, data, label):
        if len(data) > MAX_BYTES:
            self.issues.append(f"{label}: oversized object requires manual review")
            return
        try:
            text = data.decode("utf-8")
            if "\0" in text:
                raise ValueError()
        except (UnicodeError, ValueError):
            self.issues.append(f"{label}: non-text object requires manual review")
            return
        for name, pattern in PATTERNS.items():
            if pattern.search(text):
                self.issues.append(f"{label}: {name}")
        if any(term in text.casefold() for term in self.terms):
            self.issues.append(f"{label}: private marker")

    def entry(self, mode, oid, path, label):
        # Never echo paths or matching values: a path itself can disclose data.
        self.text(path, label + " filename")
        name = os.fsdecode(path).lower()
        parts = name.split("/")
        leaf = parts[-1]
        if (PRIVATE_PARTS.intersection(parts) or leaf in PRIVATE_NAMES
                or (leaf.startswith(".env") and leaf != ".env.example")
                or Path(leaf).suffix in PRIVATE_SUFFIXES):
            self.issues.append(f"{label}: private/artifact path")
        if mode not in ("100644", "100755", "120000"):
            self.issues.append(f"{label}: unsupported Git entry (including submodules)")
            return
        if oid in self.seen:
            return
        self.seen.add(oid)
        if int(git("cat-file", "-s", oid)) > MAX_BYTES:
            self.issues.append(f"{label}: oversized blob requires manual review")
        else:
            self.text(git("cat-file", "blob", oid), label + " blob " + oid[:12])

    def staged(self):
        for n, record in enumerate(git("ls-files", "--stage", "-z").split(b"\0")):
            if not record:
                continue
            header, path = record.split(b"\t", 1)
            mode, oid, stage = header.decode().split()
            if stage != "0":
                raise Refused("unmerged index; resolve it before publication")
            self.entry(mode, oid, path, f"index entry {n + 1}")

    def tree(self, oid):
        for n, record in enumerate(git("ls-tree", "-r", "-z", "--full-tree", oid).split(b"\0")):
            if not record:
                continue
            header, path = record.split(b"\t", 1)
            mode, _, blob = header.decode().split()
            self.entry(mode, blob, path, f"commit {oid[:12]} entry {n + 1}")

    def history(self, tip, bases):
        self.tree(tip)  # Always inspect the final tree, including grandfathered blobs.
        oids = git("rev-list", f"--max-count={MAX_COMMITS + 1}", tip,
                   "--not", *bases).decode().splitlines()
        if len(oids) > MAX_COMMITS:
            raise Refused("history exceeds review bound; audit and scope public bases explicitly")
        for oid in oids:
            self.text(git("cat-file", "commit", oid), "commit metadata " + oid[:12])
            self.tree(oid)  # Also catches sensitive data added then removed before push.

    def finish(self):
        if self.issues:
            for issue in self.issues[:30]:
                print("publication check: " + issue, file=sys.stderr)
            if len(self.issues) > 30:
                print("publication check: further findings omitted", file=sys.stderr)
            raise Refused("blocked; inspect the identified objects privately, not in public logs")
        print("publication check: passed scoped checks (human review still required)")


def approved_rewrite(remote_url, rows):
    """An expiring, exact ref-set approval; never an exemption from scanning."""
    path = common_dir() / "publication-rewrite.json"
    if not os.path.lexists(path):
        return False
    try:
        # No symlinks, shared-readable approvals, special files or unbounded reads.
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as source:
            info = os.fstat(source.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_mode & 0o077 or info.st_size > 65536):
                raise ValueError()
            plan = json.loads(source.read(65537))
        if not isinstance(plan, dict) or set(plan) != {"remote", "expires_at", "updates"}:
            raise ValueError()
        now = time.time()
        if (plan["remote"] != remote_url or type(plan["expires_at"]) is not int
                or not now < plan["expires_at"] <= now + 3600):
            raise ValueError()
        updates = plan["updates"]
        if not isinstance(updates, list) or not updates or len(updates) != len(rows):
            raise ValueError()
        expected = {}
        for update in updates:
            if not isinstance(update, dict) or set(update) != {"ref", "old", "new"}:
                raise ValueError()
            ref, old, new = (update[key] for key in ("ref", "old", "new"))
            if (not isinstance(ref, str) or not ref.startswith("refs/heads/")
                    or ref in expected or old == new):
                raise ValueError()
            for oid in (old, new):
                if (not isinstance(oid, str)
                        or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", oid)
                        or set(oid) == {"0"} or commit(oid) != oid):
                    raise ValueError()
            expected[ref] = (old, new)
        actual = {remote_ref: (remote_oid, local_oid)
                  for _, local_oid, remote_ref, remote_oid in rows}
        if len(actual) != len(rows) or actual != expected:
            raise ValueError()
    except (OSError, ValueError, TypeError, UnicodeError, Refused):
        raise Refused("invalid, expired or mismatched Git-local rewrite approval") from None
    return True


def check_push(scan, policy, remote_url, stream):
    if remote_url not in policy["allowed_remotes"]:
        raise Refused("push destination is not approved in Git-local policy")
    if git("rev-parse", "--is-shallow-repository").strip() != b"false":
        raise Refused("cannot audit outgoing history in a shallow repository")
    grafts = common_dir() / "info/grafts"
    if grafts.exists() and grafts.stat().st_size:
        raise Refused("remove history grafts before auditing publication")
    # Partial clones may need network reads for missing blobs; never fetch implicitly.
    if (git("config", "--get", "extensions.partialClone", ok=(0, 1)).strip()
            or git("config", "--get-regexp", r"^remote\..*\.promisor$", ok=(0, 1)).strip()):
        raise Refused("use a complete local clone for publication review")
    rows = [line.split() for line in stream]
    for fields in rows:
        if len(fields) != 4:
            raise Refused("malformed pre-push input")
        local_ref, local_oid, remote_ref, remote_oid = fields
        if remote_ref not in policy["allowed_refs"]:
            raise Refused("destination ref is not approved; tags and private refs are not published")
        if any(part.lower() in ("private", "local") for part in local_ref.split("/")):
            raise Refused("private source ref cannot be published")
        if not all(re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", oid)
                   for oid in (local_oid, remote_oid)):
            raise Refused("invalid object IDs in pre-push input")
        if set(local_oid) == {"0"}:
            raise Refused("ref deletion requires a separate history decision")
    rewrite = approved_rewrite(remote_url, rows)
    for local_ref, local_oid, remote_ref, remote_oid in rows:
        tip = commit(local_oid)
        bases = list(policy["public_bases"])
        if rewrite:
            # No grandfathered commits, even when the old tip or a private
            # ancestor was accidentally entered in public_bases.
            bases = []
        elif set(remote_oid) != {"0"}:
            # A remote ancestor is already public at this approved destination.
            base = commit(remote_oid)
            result = subprocess.run(
                ["git", "merge-base", "--is-ancestor", base, tip],
                env=dict(os.environ, GIT_NO_REPLACE_OBJECTS="1", GIT_NO_LAZY_FETCH="1"),
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if result.returncode:
                raise Refused("non-fast-forward or unreadable history; no rewrite is authorized")
            bases.append(base)
        scan.text(local_ref.encode(), "source ref")
        scan.text(remote_ref.encode(), "destination ref")
        scan.history(tip, bases)


def install_hooks():
    load_policy(required=True)
    if git("config", "--get", "core.hooksPath", ok=(0, 1)):
        raise Refused("custom hooksPath exists; integrate the guard manually without replacing it")
    common = common_dir()
    hooks = common / "hooks"
    hooks.mkdir(exist_ok=True)
    for name in ("pre-commit", "pre-push"):
        dest = hooks / name
        if dest.is_symlink() or (dest.exists() and HOOK_MARKER not in dest.read_text().splitlines()):
            raise Refused("existing hook is not owned by this installer; integrate manually")
    installed = common / "publication-checker.py"
    commands = {
        "pre-commit": "staged",
        "pre-push": 'push "$@"',
    }
    # A reviewed local snapshot works even on branches predating this tool.
    files = {installed: Path(__file__).read_bytes()}
    for name, command in commands.items():
        files[hooks / name] = (
            "#!/bin/sh\n" + HOOK_MARKER + "\nexec " + shlex.quote(sys.executable)
            + " -I " + shlex.quote(str(installed))
            + " --require-policy " + command + "\n").encode()
    for path, data in files.items():
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as out:
            temp = Path(out.name)
            out.write(data)
        temp.chmod(0o700)
        os.replace(temp, path)
    print("Installed Git-local hooks and reviewed checker snapshot; no runtime changes.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--require-policy", action="store_true")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("staged", help="scan the complete index, not the worktree")
    revision = commands.add_parser("revision", help="scan a tree and its commit metadata")
    revision.add_argument("ref")
    push = commands.add_parser("push", help="Git pre-push hook; read ref updates on stdin")
    push.add_argument("remote_name")
    push.add_argument("remote_url")
    commands.add_parser("install-hooks", help="install locally after policy and code review")
    args = parser.parse_args()
    try:
        policy = load_policy(args.require_policy or args.command in ("push", "install-hooks"))
        scan = Scan(policy)
        if args.command == "staged":
            scan.staged()
        elif args.command == "revision":
            oid = commit(args.ref)
            scan.tree(oid)
            scan.text(git("cat-file", "commit", oid), "commit metadata " + oid[:12])
        elif args.command == "push":
            check_push(scan, policy, args.remote_url, sys.stdin)
        else:
            install_hooks()
            return 0
        scan.finish()
    except (Refused, OSError, ValueError, UnicodeError):
        error = sys.exc_info()[1]
        message = str(error) if isinstance(error, Refused) else "unreadable input; check locally"
        print("publication check: " + message, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
