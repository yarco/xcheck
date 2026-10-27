"""Cline's native free offers and headless launch. No generic inference API route."""
from __future__ import annotations

import json
import platform
import shutil
import subprocess
from pathlib import Path

CATALOG = "https://api.cline.bot/api/v1/ai/cline/recommended-models"
DOCS = "https://docs.cline.bot/getting-started/free-models"
DATA = Path.home() / ".cline/data"
FLAGS = ("--json", "--provider", "--model", "--config", "--cwd", "--auto-approve", "--thinking", "--retries")


def installation() -> tuple[Path, Path]:
    """Use the npm package's native executable: its JS wrapper drops lock fds."""
    command = shutil.which("cline")
    if not command:
        raise ValueError("Cline CLI is not installed; install `cline` with npm")
    executable = Path(command).resolve()
    roots = [executable.parent.parent, *(p / "cline" for p in executable.parents if p.name == "node_modules")]
    root = next((p for p in roots if (p / "package.json").is_file()
                 and json.loads((p / "package.json").read_text()).get("name") == "cline"), None)
    if root is None:
        raise ValueError("Cline npm package could not be identified")
    system = {"Darwin": "darwin", "Linux": "linux"}[platform.system()]
    arch = {"arm64": "arm64", "aarch64": "arm64", "x86_64": "x64"}[platform.machine()]
    binary = module_dir(root, f"@cline/cli-{system}-{arch}") / "bin/cline"
    if not binary.is_file():
        raise ValueError("Cline native executable is missing from its npm package")
    return root, binary


def module_dir(root: Path, name: str) -> Path:
    for parent in (root, *root.parents):
        candidate = parent / "node_modules" / name
        if (candidate / "package.json").is_file():
            return candidate
    raise ValueError(f"Cline npm dependency {name} is unavailable")


def free_rows(payload: dict) -> list[dict]:
    """Only explicit free wire IDs in the live free bucket authorize a turn."""
    from .adapters import parse_lane
    if not isinstance(payload, dict) or not isinstance(payload.get("free"), list):
        raise ValueError("Cline free-model feed changed")
    seen, rows = set(), []
    for item in payload["free"]:
        ident = item.get("id") if isinstance(item, dict) else None
        if not isinstance(ident, str) or ident in seen:
            raise ValueError("Cline free-model feed has malformed or duplicate IDs")
        seen.add(ident)
        lane = parse_lane(f"cline:{ident}")
        # A general paid model briefly featured as free could later bill under
        # the same selector. Dedicated free routes instead stop at their quota.
        dedicated = ident.startswith("cline-free/") or ident.endswith(("-free", ":free"))
        if lane.model != ident or not dedicated:
            continue
        rows.append({"model": ident, "name": item.get("name") or ident,
                     "free": True, "price": "zero", "free_access": True})
    return rows


def auth_problem() -> str | None:
    """Read native OAuth readiness only; never return a token or custom route."""
    try:
        stored = json.loads((DATA / "settings/providers.json").read_text())
        entry = stored["providers"]["cline"]
        settings = entry["settings"]
        if (entry.get("tokenSource") != "oauth" or settings.get("provider") != "cline"
                or not isinstance(settings.get("auth", {}).get("accessToken"), str)
                or not settings["auth"]["accessToken"].strip()):
            raise ValueError("no OAuth login")
        if (settings.get("baseUrl") not in (None, "", "https://api.cline.bot/api/v1")
                or settings.get("headers") or settings.get("apiKey") or settings.get("auth", {}).get("apiKey")):
            return "auth-required: Cline has custom routing/API-key settings; use native Cline OAuth"
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return "auth-required: no native Cline OAuth login; run `cline auth cline`"
    return None


def metadata(payload: dict) -> dict:
    """The installed SDK's live catalog, using this exact provider observation.

    Its model capabilities are advisory (including vendor defaults for new
    offers). Actual file access/completion is established by qualification.
    Neither the SDK's cached models nor Models.dev establish free pricing.
    """
    from .adapters import Launch
    from .lane import lane_env
    root, _ = installation()
    script = """
import {pathToFileURL} from 'node:url';
const {fetchLiveProviderModels} = await import(pathToFileURL(process.argv[1]).href);
let body = ''; for await (const part of process.stdin) body += part;
const feed = JSON.parse(body);
const fetcher = (url, options) => String(url).endsWith('/ai/cline/recommended-models')
  ? Promise.resolve(new Response(JSON.stringify(feed)))
  : fetch(url, {...options, signal: AbortSignal.timeout(20000)});
const catalog = await fetchLiveProviderModels('https://models.dev/api.json', fetcher);
console.log(JSON.stringify(catalog.cline ?? {}));
"""
    spec = Launch([], None, kind="cline")
    out = subprocess.run([shutil.which("node") or "node", "--input-type=module", "-e", script,
                          str(module_dir(root, "@cline/llms") / "dist/index.js")],
                         input=json.dumps(payload), env=lane_env(spec, root), cwd=root,
                         capture_output=True, text=True, timeout=40)
    if out.returncode:
        raise ValueError("Cline native SDK model inventory failed")
    result = json.loads(out.stdout)
    if not isinstance(result, dict):
        raise ValueError("Cline native SDK returned a non-mapping catalog")
    return result


def discover(payload: dict) -> tuple[list[dict], dict]:
    rows = free_rows(payload)
    read = {"status": "ok", "rows": rows,
            "served_ids": [item["id"] for item in payload["free"]]}
    try:
        catalog = metadata(payload) if rows else {}
        problem = auth_problem()
        read["client_ids"] = sorted(catalog)
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        catalog, problem = {}, "needs-client-setup"
        read["client_error"] = type(error).__name__
    offers = []
    for row in rows:
        local = catalog.get(row["model"]) or {}
        caps = local.get("capabilities") or []
        modalities = local.get("modalities") or {"input": ["text"], "output": ["text"]}
        suitable = ("tools" in caps and "text" in modalities.get("input", [])
                    and modalities.get("output") == ["text"] and local.get("operation", "language") == "language")
        if local and not suitable:
            continue
        offers.append({"model": row["model"], "name": row["name"], "provider": "cline",
                       "selector": f"cline:{row['model']}", "release_date": local.get("releaseDate", ""),
                       "date_source": "Cline native SDK; advisory", "tool_call": suitable,
                       "source": CATALOG, "pricing_source": CATALOG, "provider_docs": DOCS,
                       "auth_env": [], "client_available": bool(local),
                       "state": "needs-client-setup" if not local else "auth-required" if problem else "ready",
                       "access": "Native Cline OAuth; limited free quota; usage may improve models",
                       "model_key": f"cline/{row['model']}"})
    return offers, read


def launch(lane, work: Path, message: Path, log: Path):
    from . import adapters, models
    from .files import ROOT
    try:
        _, binary = installation()
        rows = free_rows(models.fetch(CATALOG))
        if lane.model not in {row["model"] for row in rows}:
            raise ValueError("selected model is not a current dedicated Cline free offer")
        if problem := auth_problem():
            raise ValueError(problem)
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        raise adapters.Unavailable(f"inventory-unknown: Cline admission refused ({error})") from error
    config = log.parent / "cline-config"
    config.mkdir(parents=True, exist_ok=True)
    # ACP's inventory omits the dedicated free routes (3.0.74). The native
    # headless command accepts their exact wire IDs without catalog fallback.
    # Keep OAuth refresh in the original store; sessions/config are lane-local.
    argv = [str(binary), "--json", "--provider", "cline", "--model", lane.model,
            "--cwd", str(work), "--config", str(config), "--auto-approve", "true", "--retries", "1"]
    if lane.effort:
        argv.extend(["--thinking", lane.effort])
    argv.extend(["--", message.read_text()])
    return adapters.Launch(argv, None, env_set={"XCHECK_ROOT": str(ROOT),
        "CLINE_DATA_DIR": str(config / "data"),
        "CLINE_PROVIDER_SETTINGS_PATH": str(DATA / "settings/providers.json")}, kind="cline")


def stderr_errors(reader, path: Path) -> None:
    """The native CLI publishes pre-run failures as JSON on stderr."""
    try:
        with path.open('rb') as handle:
            handle.seek(0, 2)
            handle.seek(max(0, handle.tell() - 32000))
            lines = handle.read().decode('utf-8', errors='replace').splitlines()
    except OSError:
        return
    for line in lines:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get('type') == 'error':
            reader.feed(event)
