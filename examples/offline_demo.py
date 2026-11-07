#!/usr/bin/env python3
"""Exercise installation and collection with fake CLIs and a disposable HOME."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from stage_demo import stage


def main() -> None:
    if sys.version_info < (3, 12):
        sys.exit("Use Python 3.12 or newer")
    repo = Path(__file__).resolve().parent.parent
    with tempfile.TemporaryDirectory(prefix="xcheck-offline-") as temporary:
        root = Path(temporary).resolve()
        home, binary = root / "home", root / "bin"
        home.mkdir()
        binary.mkdir()
        for name in ("codex", "claude"):
            (binary / name).symlink_to(repo / "tests/fake/harness")
        (binary / "python3").symlink_to(sys.executable)
        config = home / ".config/xcheck/config.json"
        config.parent.mkdir(parents=True)
        shutil.copyfile(repo / "examples/config.example.json", config)
        # Do not inherit credentials, identity, shell wrappers, or provider config.
        env = {"HOME": str(home), "PATH": f"{binary}:/usr/bin:/bin",
               "XCHECK_ROOT": str(root / "runs"), "FAKE_MODE": "ok",
               "LANG": "en_US.UTF-8"}
        def run(*command):
            result = subprocess.run(command, env=env, cwd=root, capture_output=True,
                                    text=True, timeout=60)
            if result.returncode:
                raise RuntimeError(result.stdout + result.stderr)
            return result

        run(sys.executable, "-I", str(repo / "bin/xcheck"), "install")
        entry = str(home / ".local/bin/xcheck")
        base = root / "runs/demo"
        stage(base)
        run(entry, "run", "--base", str(base), "--orch", "other",
            "--mode", "headless", "--web", "packet", "--skip-capacity")
        rows = json.loads((base / "STATUS.json").read_text())["lanes"]
        if len(rows) != 2 or any(row["status"] != "done" for row in rows):
            raise RuntimeError(f"unexpected collection result: {rows}")
        for row in rows:
            answer = (base / f"RESULT-{row['lane']}.md").read_text()
            if not answer.rstrip().endswith("RESULT_COMPLETE"):
                raise RuntimeError("missing answer commitment")
        snapshot = home / ".local/share/xcheck/current"
        if list(snapshot.rglob("config.json")) or (snapshot / "local").exists():
            raise RuntimeError("local configuration entered the runtime snapshot")
        print("PASS: isolated installation, local roster, two fake lanes, native completion, proof validation.")
        print("No real model, login, network retrieval, or review judgment was exercised.")


if __name__ == "__main__":
    main()
