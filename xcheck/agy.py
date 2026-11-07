"""Inference-free Antigravity inventory, shared by admission and doctor."""
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from . import adapters, models
from .environment import lane_env

PROBE_MODEL = "xcheck-doctor-no-such-model"
REGISTRY = "https://raw.githubusercontent.com/agentclientprotocol/registry/main/antigravity-acp/agent.json"


def registry_version() -> str | None:
    try:
        listed = models.fetch(REGISTRY)
    except Exception:
        return None
    version = listed.get("version") if isinstance(listed, dict) else None
    return version if isinstance(version, str) and re.fullmatch(r"[0-9][0-9A-Za-z.+-]*", version) else None


def inventory() -> list[str] | None:
    """An unknown selector stops acpx after session/new, before a model turn."""
    with tempfile.TemporaryDirectory(prefix="xcheck-agy-doctor-") as tmp:
        work, home = Path(tmp) / "work", adapters._agy_home(Path(tmp) / "home", web=False)
        work.mkdir()
        (home / "config" / "mcp_config.json").write_text('{"mcpServers": {}}')
        # Without a staged login the server would open browser sign-in, even
        # for this inventory-only probe. Never let it fall back to Keychain.
        try:
            adapters.agy_login(home)
        except adapters.Unavailable:
            return None
        cmd = [shutil.which("acpx") or "acpx", "--agent", adapters._acpx_quote(str(adapters.AGY_ACP_SERVER)),
               "--cwd", str(work), "--model", PROBE_MODEL, "--approve-all", "--no-terminal", "--no-fs",
               "--format", "json", "exec", "x"]
        try:
            spec = adapters.Launch(cmd, None, env_set={"GEMINI_HOME": str(home), "TMPDIR": str(home / "tmp"),
                                                       **adapters.AGY_ENV})
            out = subprocess.run(cmd, cwd=work, env=lane_env(spec, work), capture_output=True,
                                 text=True, timeout=120, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.TimeoutExpired):
            return None
    found = re.search(r'Available models: ([^"\n]+)', out.stdout + out.stderr)
    if not found:
        return None
    # A terminal period followed by whitespace starts prose, not an identifier.
    sentence = re.split(r"\.\s", found.group(1).strip(), maxsplit=1)[0].rstrip(".")
    identifiers = [m.strip() for m in sentence.split(",")]
    return identifiers if all(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", m) for m in identifiers) else None
