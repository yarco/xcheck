"""One disposable home for the whole test session, set before xcheck is imported.

xcheck reads the operator's ~/.claude.json, ~/.acpx/config.json,
~/.config/devin/config.json, ~/.codex and ~/.grok, and the Agy server under
~/.local, some of them at import time. Tests must neither depend on nor touch
the real ones, so every worker gets an empty home holding only a Grok login
stub and the fake Agy server."""

import atexit
import os
import shutil
import sys
import tempfile
from pathlib import Path

if sys.version_info < (3, 12):  # xcheck needs 3.12+; bin/xcheck would re-exec and replace the worker
    sys.exit(f"tests need Python 3.12+ (this is {sys.version.split()[0]}): uv run --python '>=3.12' ...")

HOME = Path(tempfile.mkdtemp(prefix="xcheck-test-home-"))
atexit.register(shutil.rmtree, HOME, True)
os.environ["HOME"] = str(HOME)
for name in ("XCHECK_GROK_HOME", "XCHECK_AGY_ACP_DIR", "XCHECK_ROOT"):
    os.environ.pop(name, None)
(HOME / ".grok").mkdir()
(HOME / ".grok" / "auth.json").write_text("{}")
agy = HOME / ".local" / "lib" / "antigravity-acp"
agy.mkdir(parents=True)
(agy / "agy_acp_server.par").symlink_to(Path(__file__).resolve().parent / "fake" / "harness")
