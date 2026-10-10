"""Per-harness knowledge: how to start a lane, and how to read what it streams.

Every flag here was checked against CLI help and smoke runs. Codex ACP was
verified with codex-cli 0.162.0, the reviewed codex-acp 2.1.1 automation build,
and acpx 0.19.4. `xcheck doctor` re-checks flags against installed help.

Stream lanes print their answer as a final message; the lane runner extracts
that text from the harness's structured stream. Grok may write scratch files
in its workspace while reviewing.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

KINDS = ("codex", "claude", "grok", "agy", "opencode", "devin")

@dataclass(frozen=True)
class Lane:
    kind: str
    model: str
    effort: str = ""  # empty for agy/devin; optional OpenCode ACP effort

    @property
    def name(self) -> str:
        """Provenance name: harness + exact model (+ effort when it is a flag)."""
        model = self.model.replace("/", "-").replace(":", "-")
        base = model if model.startswith(self.kind) else f"{self.kind}-{model}"
        if self.effort and not base.endswith(f"-{self.effort}"):
            base = f"{base}-{self.effort}"
        return base

    @property
    def pool(self) -> str:
        if self.kind == "agy":
            return "agy-gemini"
        if self.kind == "opencode" and "/" in self.model:
            provider = self.model.split('/', 1)[0]
            return "opencode" if provider == "opencode" else f"opencode-{provider}"
        return self.kind

    def to_json(self) -> dict:
        return {"kind": self.kind, "model": self.model, "effort": self.effort}

    @classmethod
    def from_json(cls, data: dict) -> "Lane":
        return cls(data["kind"], data["model"], data.get("effort", ""))


DEFAULT_ROSTER = (
    Lane("codex", "gpt-6.1-sol", "high"),
    Lane("claude", "claude-opus-5-5", "high"),
    Lane("grok", "grok-4.7", "xhigh"),
    Lane("agy", "gemini-pro-agent"),  # the ACP server's id for Gemini 3.1 Pro (High)
    Lane("agy", "gemini-3.8-flash-high"),
)

# Supported but never default (`run --add`). Pins are the operator's current
# extras; they can be dropped from a roster without changing the protocol.
# The opencode pins are zero-price offers confirmed by `xcheck models`; keep
# discovering other offers with it rather than pinning more. A vanished free
# offer fails doctor until the pin is refreshed.
OPT_IN = (
    Lane("devin", "swe-2-max"),
    Lane("opencode", "opencode/space-bunny-free"),
    # Contributor tier permits logging/training; mind what its packets hold.
    Lane("opencode", "opencode/muse-spark-1.3-contributor-free"),
)


def parse_lane(spec: str) -> Lane:
    """`kind:model[:effort]` → Lane. Agy and Devin take no effort.
    Opencode models may be `provider/model` (a slash is part of the model)."""
    parts = spec.split(":")
    # OpenRouter's :free is part of the model, never an effort. Preserve it
    # before interpreting the optional final variant (including :free:high).
    if parts[0] == "opencode" and len(parts) >= 3 and parts[2] == "free":
        parts = [parts[0], parts[1] + ":free", *parts[3:]]
    if len(parts) not in (2, 3) or parts[0] not in KINDS or not parts[1]:
        raise ValueError(f"lane spec must be kind:model[:effort], kind in {KINDS}: {spec!r}")
    kind, model = parts[0], parts[1]
    if kind == "agy" and not model.startswith("gemini-"):
        raise ValueError("Agy's ACP server supports only Gemini selectors")
    effort = parts[2] if len(parts) == 3 else ""
    ident = r"[A-Za-z0-9][A-Za-z0-9._/-]*(?::free)?" if kind == "opencode" else r"[A-Za-z0-9][A-Za-z0-9._-]*"
    for part in (model, effort):
        if part and not re.fullmatch(ident if part is model else r"[A-Za-z0-9][A-Za-z0-9._-]*", part):
            raise ValueError(f"model and effort must be plain identifiers: {part!r}")
    if kind in ("agy", "devin") and effort:
        raise ValueError(f"{kind} selectors carry their own effort; drop ':{effort}'")
    if kind not in ("agy", "opencode", "devin") and not effort:
        raise ValueError(f"{kind} lane needs an explicit effort: {spec!r}")
    return Lane(kind, model, effort)


class Unavailable(Exception):
    """A lane cannot start here; found while building its command, before spend."""


@dataclass
class Launch:
    argv: list[str]
    stdin: Path | None  # file to feed on stdin, or None for /dev/null
    env_set: dict[str, str] = field(default_factory=dict)
    kind: str = ""  # lane kind, for kind-scoped env scrubbing


def launch(lane: Lane, work: Path, message: Path, final: Path, log: Path, web: bool) -> Launch:
    """Headless command for one lane (stream view). `work` is its workspace,
    `log` its forensic log."""
    if lane.kind == "codex":
        return _codex_launch(lane, work, message, log, web)
    if lane.kind == "claude":
        tools = "Read,WebSearch,WebFetch" if web else "Read"
        argv = [
            "claude", "-p", "--restricted", *claude_mcp(web), "--permission-mode", "dontAsk",
            # With -p, anything that would prompt is denied instead of waiting.
            "--permission-prompts", "none",
            "--tools", tools, "--allowedTools", ",".join([tools, *claude_mcp_allow(web)]), "--add-dir", str(work),
            "--no-session-persistence", "--model", lane.model, "--effort", lane.effort,
            "--debug-file", str(log),
            # Partial messages expose native output during a long think.
            "--output-format", "stream-json", "--verbose", "--include-partial-messages",
        ]
        return Launch(argv, message, env_set=CLAUDE_ENV)
    if lane.kind == "grok":
        return _grok_launch(lane, work, message, log, web)
    if lane.kind == "agy":
        return _agy_launch(lane, work, message, log, web)
    if lane.kind == "devin":
        return _devin_launch(lane, work, message, log, web)
    if lane.kind == "opencode":
        return _opencode_launch(lane, work, message, log)
    raise ValueError(f"{lane.kind} has no headless form; it runs as its app (tui)")


CODEX_ACP_VERSION = "2.1.1"  # re-verify permissions and native events before changing


def _codex_launch(lane: Lane, work: Path, message: Path, log: Path, web: bool) -> Launch:
    from .files import ROOT
    base = str(log.parent.parent.parent)
    agent = [sys.executable, "-I", str(BIN), "hold", "--pool", lane.pool,
             "--run", base, "--log", str(log), "--", shutil.which("codex-acp") or "codex-acp"]
    argv = [shutil.which("acpx") or "acpx", "--agent", " ".join(_acpx_quote(a) for a in agent),
            "--cwd", str(work), "--model", lane.model, "--deny-all", "--auth-policy", "skip",
            "--no-terminal", "--no-fs", "--prompt-retries", "0", "--format", "json", "exec",
            "--config-option", "mode=read-only", "--config-option", f"reasoning_effort={lane.effort}",
            "--file", str(message)]
    return Launch(argv, None, env_set={
        "XCHECK_ROOT": str(ROOT), "XCHECK_CODEX_RUN": base,
        "XCHECK_CODEX_BINARY": shutil.which("codex") or "codex",
        # Node drops inherited descriptors again when the adapter spawns Codex.
        # The installed xcheck entry point rejoins the same pool before exec.
        "CODEX_PATH": str(BIN), "INITIAL_AGENT_MODE": "read-only", "NO_BROWSER": "1",
        "CODEX_ACP_EPHEMERAL": "1",
        "CODEX_CONFIG": json.dumps({"model": lane.model, "model_reasoning_effort": lane.effort,
                                     "sandbox_mode": "read-only", "approval_policy": "never",
                                     "web_search": "live" if web else "disabled",
                                     "check_for_update_on_startup": False,
                                     "history": {"persistence": "none"}}),
        "APP_SERVER_LOGS": str(log.parent / "codex-logs"),
    })


def codex_acp_problem() -> str | None:
    """Only the explicitly installed, audited adapter; never an npm caret launch."""
    if not shutil.which("codex") or not shutil.which("codex-acp"):
        return ("startup-unavailable: codex and the reviewed codex-acp automation build must be on PATH; "
                "see integrations/codex-acp/README.md (stock npm is insufficient)")
    from .lane import lane_env
    try:
        out = subprocess.run([shutil.which("codex-acp"), "--automation-capabilities"],
                             env=lane_env(Launch([], None, env_set={"CODEX_PATH": os.devnull}), Path.cwd()),
                             stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as exc:
        return f"startup-unavailable: codex-acp capability probe failed ({type(exc).__name__})"
    try:
        features = json.loads(out.stdout)
    except ValueError:
        features = None
    if out.returncode or features != {"version": CODEX_ACP_VERSION, "ephemeralSessions": True, "failedTurnErrors": True}:
        return (f"startup-unavailable: codex-acp needs the reviewed {CODEX_ACP_VERSION} automation build "
                "(ephemeral sessions and failed-turn errors); stock npm releases lack these controls")
    return None


def _opencode_model(lane: Lane) -> list[str]:
    model = lane.model if "/" in lane.model else f"opencode/{lane.model}"
    return ["--model", model]


def _opencode_launch(lane: Lane, work: Path, message: Path, log: Path) -> Launch:
    from .files import ROOT
    agent = [sys.executable, "-I", str(BIN), "hold", "--pool", lane.pool,
             "--run", str(log.parent.parent.parent), "--log", str(log), "--",
             shutil.which("opencode") or "opencode", "acp", "--cwd", str(work)]
    argv = [shutil.which("acpx") or "acpx", "--agent", " ".join(_acpx_quote(a) for a in agent),
            "--cwd", str(work), *_opencode_model(lane), "--approve-all", "--auth-policy", "skip",
            "--no-terminal", "--no-fs", "--prompt-retries", "0", "--format", "json", "exec",
            "--config-option", "mode=build"]
    spec = Launch(argv, None, env_set={**_opencode_env(lane), "XCHECK_ROOT": str(ROOT)})
    effort = lane.effort
    if not effort:
        # Unset, ACP selects the model's first (lowest) variant; `default`
        # leaves the provider's own effort. Models without variants have no
        # effort option to set.
        from .lane import lane_env
        from .models import client_metadata
        model = _opencode_model(lane)[1]
        try:
            metadata = client_metadata(refresh=False, env=lane_env(spec, work), cwd=work)
        except (OSError, subprocess.SubprocessError, ValueError) as e:
            raise Unavailable(f"startup-unavailable: `opencode models --verbose` failed ({_clip(str(e), 200)})") from e
        if model not in metadata:
            raise Unavailable(f"startup-unavailable: {model} is not in `opencode models --verbose`; "
                              "connect its provider or pick a listed model")
        if metadata[model].get("variants"):
            effort = "default"
    argv.extend([*(["--config-option", f"effort={effort}"] if effort else []), "--file", str(message)])
    return spec


def missing_keys(lane: Lane) -> list[str]:
    """Provider keys this lane cannot run without that are empty here. Without
    one the provider fails only at the first request with a bare server error;
    a launchd session exports ZAI_API_KEY empty."""
    import os
    if lane.kind == "opencode" and lane.model.split("/", 1)[0].startswith("zai") and "/" in lane.model:
        return [n for n in ("ZAI_API_KEY",) if not os.environ.get(n)]
    return []


def _opencode_env(lane: Lane) -> dict[str, str]:
    """Provider-prefixed opencode models may need that provider's env key.
    Stored opencode auth.json covers OpenRouter; Z.ai Coding Plan needs
    ZAI_API_KEY and ZHIPU_API_KEY (stripping the latter yields a server error)."""
    import os
    env = {}
    if "/" not in lane.model:
        return env
    provider = lane.model.split("/", 1)[0]
    names = []
    if provider.startswith("zai"):
        names.extend(["ZAI_API_KEY", "ZHIPU_API_KEY"])  # coding-plan talks Zhipu
    if provider == "openrouter":
        names.append("OPENROUTER_API_KEY")
    for name in names:
        v = os.environ.get(name)
        if v:
            env[name] = v
    return env


# Grok reviews run Grok's documented native ACP server (`grok agent … stdio`)
# through acpx, in the same hold custody as the other ACP lanes:
#   acpx --no-terminal --no-fs        transport only; no client-side fs or shell
#   xcheck hold                     re-joins this run's pool (acpx passes only stdio)
#   grok agent stdio                the vendor server; --always-approve resolves
#                                   permissions in-agent, as the stream showed
# The lane gets its own GROK_HOME beside the harness log: the operator's
# config.toml registers paid-capable and credential-bearing MCP servers
# (keenable and parallel-search carry auth headers) and session/new mcpServers
# only adds to that set, so inheriting it would leak both. The lane config
# pins the review contract and copies only the keyless MCP entries verbatim.
# Auth stays in the operator's real ~/.grok/auth.json via GROK_AUTH_PATH, so
# subscription custody and token refresh live in exactly one file; nothing
# secret is copied into the run. Sessions persist under the lane home, which
# keeps them out of the operator's session registry too.
# The unrestricted sandbox is the operator-authorized policy: read-only
# refuses OrbStack's symlinked Docker socket. Write/MCP limits are an
# instruction, not an OS boundary — the same honest claim the native path made.
# Kept current by the operator's own login; XCHECK_GROK_HOME points tests at a fake.
GROK_DIR = Path(os.environ.get("XCHECK_GROK_HOME") or Path.home() / ".grok")
GROK_AUTH_FILE = GROK_DIR / "auth.json"
GROK_CONFIG_FILE = GROK_DIR / "config.toml"
GROK_REQUIREMENTS = GROK_DIR / "requirements.toml"
# Operator-config MCP names a review may use: local commands or keyless HTTP
# endpoints. perplexity bills Pro searches, composio connects arbitrary apps,
# and keenable/parallel-search carry credentials — all excluded.
GROK_MCP_ALLOWED = ("qmd", "semble", "deepwiki")
GROK_REVIEW_TOOLS = ("read_file", "grep", "list_dir", "run_terminal_cmd")
GROK_WEB_TOOLS = ("web_search", "web_fetch")
# The native version this adapter's tool ids, config keys and ACP shape were
# verified against (see integrations/grok-acp/README.md). An unknown tool id
# makes the profile allowlist fail OPEN to the full toolset, so lanes refuse
# to start on any other version until it is revalidated — a string compare
# on the parsed `version --json` value, never a substring match.
GROK_VERIFIED_VERSION = "1.0.50 (c58f321264ba)"
GROK_ENV = {
    # A subscription session, never a paid API-key fallback (XAI_API_KEY is
    # scrubbed anyway; this refuses to use it if one ever leaked in).
    "GROK_DISABLE_API_KEY_AUTH": "1",
    "GROK_DISABLE_AUTOUPDATER": "1", "GROK_TELEMETRY_ENABLED": "0",
    # No delegation, memory, or side sampling. Grok still makes exactly one
    # title-generation call after the first prompt — the same one the headless
    # path made; every other background sampler is off (see
    # integrations/grok-acp/README.md).
    "GROK_SUBAGENTS": "0", "GROK_MEMORY": "0",
    "GROK_TITLE_REFRESH": "0", "GROK_TURN_SUMMARY": "0", "GROK_SESSION_RECAP": "0",
    "GROK_SESSION_SEARCH": "0",
    # Imported vendor integrations can carry MCP credentials, executable
    # hooks and orchestrator instructions; the lane config is the only source.
    **{f"GROK_{v}_{k}_ENABLED": "0" for v in ("CLAUDE", "CURSOR", "CODEX")
       for k in ("MCPS", "HOOKS", "RULES", "SKILLS", "AGENTS", "SESSIONS")},
    "GROK_MANAGED_MCPS_ENABLED": "0", "GROK_MANAGED_MCP_GATEWAY_TOOLS_ENABLED": "0",
    "GROK_OFFICIAL_MARKETPLACE_AUTO_REGISTER": "0",
}
GROK_SANDBOX_ARGS = ("--sandbox", "off")


def _toml_key(k: str) -> str:
    return k if re.fullmatch(r"[A-Za-z0-9_-]+", k) else json.dumps(k)


def _toml_value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, str):
        return json.dumps(v, ensure_ascii=False)
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    if isinstance(v, dict):
        return "{ " + ", ".join(f"{_toml_key(k)} = {_toml_value(x)}" for k, x in v.items()) + " }"
    raise TypeError(f"no TOML rendering for {type(v).__name__}")


def _grok_operator_config() -> dict:
    try:
        return tomllib.loads(GROK_CONFIG_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _grok_mcp() -> dict[str, dict]:
    """Keyless MCP entries copied verbatim from the operator's config.

    An allowlisted name is dropped if the entry gained any credential channel;
    a lane must never inherit a key it was not designed to hold."""
    cfg = _grok_operator_config()
    servers = {}
    for name in GROK_MCP_ALLOWED:
        s = (cfg.get("mcp_servers") or {}).get(name)
        if not isinstance(s, dict):
            continue
        if s.get("headers") or s.get("env") or s.get("env_vars"):
            continue
        servers[name] = s
    return servers


def _grok_rule_dirs() -> list[str]:
    """The operator's `[paths] extra_rule_dirs` — the canonical shared rules
    store (`~/.agents/rules`), referenced verbatim so lanes resolve the same
    files. Non-string entries are dropped."""
    paths = (_grok_operator_config().get("paths") or {})
    return [d for d in (paths.get("extra_rule_dirs") or []) if isinstance(d, str)]


def _grok_disabled_plugins(home: Path | None = None) -> list[str]:
    """Names of plugins the operator enabled in vendor plugin stores.

    Grok scans `~/.claude/plugins` (and sibling vendor stores) on every
    launch, honors the vendor's own enabled-state file, and loads the
    plugins' executable hooks; there is no supported global plugin kill —
    `plugins.disabled` takes explicit names/IDs only, `["*"]` does not
    match. Each store's `installed_plugins.json` keys are `name@store`; the
    bare name is an accepted `plugins.disabled` entry (verified: loading
    stops, discovery still counts the plugin)."""
    names = set()
    for vendor in ("claude", "cursor", "codex"):
        f = (home or Path.home()) / f".{vendor}/plugins/installed_plugins.json"
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        names.update(k.split("@", 1)[0] for k in (data.get("plugins") or {}) if isinstance(k, str))
    return sorted(names)


def _grok_home(home: Path, lane: Lane, web: bool) -> Path:
    """The lane's GROK_HOME: its own config, sessions, caches and plugins dir."""
    home.mkdir(parents=True, exist_ok=True)
    if GROK_REQUIREMENTS.is_file():
        # The enterprise pin layer must survive isolation, not be sidestepped.
        shutil.copy2(GROK_REQUIREMENTS, home / "requirements.toml")
    lines = [
        "# Written by xcheck for one Grok review lane. See integrations/grok-acp/README.md.",
        # Root-level key (struct Config), not a [features] entry: it must sit
        # before the first table header or TOML would bind it to the last one.
        f"disable_web_search = {_toml_value(not web)}", "",
        "[cli]", "auto_update = false", "use_leader = false", "",
        "[features]", "telemetry = false", "title_refresh = false", "turn_summary = false",
        "session_recap = false", "session_search = false", f"web_fetch = {_toml_value(web)}", "",
        "[models]", f"default = {_toml_value(lane.model)}",
        f"default_reasoning_effort = {_toml_value(lane.effort)}", "",
        "[ui]", 'permission_mode = "always-approve"', "",
        # The authorized unrestricted policy. The generated file is the pin:
        # GROK_SANDBOX itself is scrubbed globally, so no env value can
        # override it, and env cannot carry it anyway (see SCRUB_NAMES).
        "[sandbox]", 'profile = "off"', "",
        "[harness]", "disable_workspace_teleport = true", "",
        "[subagents]", "enabled = false", "",
        "[memory]", "enabled = false", "",
        # The operator's extra_rule_dirs are the canonical shared rules
        # (~/.agents/rules), loaded by reference — never copied. This is not
        # a vendor compat import: `compat.*.rules` stays off while the
        # explicit [paths] list is preserved verbatim. extra_skill_dirs is
        # deliberately not carried (skill-injection record for /import-claude).
        *(["[paths]", f"extra_rule_dirs = {_toml_value(rules)}", ""]
          if (rules := _grok_rule_dirs()) else []),
        # Vendor plugin hooks execute real commands in-agent; disable every
        # installed plugin by name (no wildcard exists). `enabled` stays
        # unset: project plugins already default off.
        "[plugins]", f"disabled = {_toml_value(_grok_disabled_plugins())}", "",
        # No operator marketplace sources; the official one is env-gated.
        "[marketplace]", "sources = []",
    ]
    for name, server in _grok_mcp().items():
        lines += ["", f"[mcp_servers.{_toml_key(name)}]",
                  *(f"{_toml_key(k)} = {_toml_value(v)}" for k, v in server.items())]
    (home / "config.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return home


def _grok_profile(path: Path, web: bool) -> Path:
    """The reviewed agent profile: an exact tool allowlist, nothing more.

    `tools` are Grok's internal tool ids (the headless --tools names); the
    doc's friendlier names fail open to the full toolset, so this list is
    asserted by tests and verified in the agent's debug log."""
    tools = ", ".join((*GROK_REVIEW_TOOLS, *(GROK_WEB_TOOLS if web else ())))
    path.write_text("---\n"
                    "name: xcheck-review\n"
                    "description: Review-only agent profile for multi-model cross-check lanes.\n"
                    "prompt_mode: full\n"
                    "agents_md: true\n"
                    f"tools: [{tools}]\n"
                    "---\n\n"
                    "You are one reviewer in a multi-model code review. Follow the packet instructions exactly.\n",
                    encoding="utf-8")
    return path


def _grok_launch(lane: Lane, work: Path, message: Path, log: Path, web: bool) -> Launch:
    from .files import ROOT
    d = log.parent
    home = _grok_home(d / "grok-home", lane, web)
    agent = [sys.executable, "-I", str(BIN), "hold", "--pool", lane.pool,
             "--run", str(d.parent.parent), "--log", str(log), "--",
             shutil.which("grok") or "grok", "agent",
             "--always-approve", "--no-leader", "-m", lane.model,
             "--reasoning-effort", lane.effort,
             "--agent-profile", str(_grok_profile(d / "grok-profile.md", web)),
             "--debug-file", str(d / "grok-agent.log"), "stdio"]
    argv = [shutil.which("acpx") or "acpx", "--agent", " ".join(_acpx_quote(a) for a in agent),
            "--cwd", str(work), "--model", lane.model,
            # --always-approve resolves tool permission in-agent; --approve-all
            # is the acpx-side equivalent for any request that still arrives.
            "--approve-all", "--auth-policy", "skip", "--no-terminal", "--no-fs",
            "--prompt-retries", "0", "--format", "json", "exec",
            "--config-option", f"reasoning_effort={lane.effort}", "--file", str(message)]
    return Launch(argv, None, env_set={**GROK_ENV, "GROK_HOME": str(home),
                                       "GROK_AUTH_PATH": str(GROK_AUTH_FILE), "XCHECK_ROOT": str(ROOT)},
                  kind="grok")


# Native accept-edits keeps the app path's workspace-edit permissions. Rules
# approve shell/MCP/fetch; acpx denies any remaining permission request. The
# shell's workspace limit is still an instruction, as it was in the app path.
DEVIN_ENV = {"DEVIN_PERMISSION_MODE": "accept-edits", "DEVIN_REFUSAL_FALLBACK": "", "DEVIN_SANDBOX": "false"}
DEVIN_USER_CONFIG = Path.home() / ".config/devin/config.json"


DEVIN_ALLOW = ["exec", "mcp__*"]
DEVIN_FETCH = ["Fetch(https://*/*)", "Fetch(http://*/*)"]


def _devin_config(path: Path, web: bool) -> Path:
    """Operator config with hooks removed, beside the harness log.
    --config replaces the whole file: stripping login/setup made the client
    ask to use local credentials. Empty hooks only, so moshi/Herdr
    PermissionRequest cannot hang the lane (I3). Never --cloud."""
    src: dict = {}
    try:
        src = json.loads(DEVIN_USER_CONFIG.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    src["hooks"] = {}
    perms = src.setdefault("permissions", {})
    perms["allow"] = [*perms.get("allow", []), *DEVIN_ALLOW, *(DEVIN_FETCH if web else [])]
    if not web:
        perms["deny"] = [*perms.get("deny", []), *DEVIN_FETCH]
    path.write_text(json.dumps(src), encoding="utf-8")
    return path


def _devin_launch(lane: Lane, work: Path, message: Path, log: Path, web: bool) -> Launch:
    from .files import ROOT
    config = _devin_config(log.with_name("devin-config.json"), web)
    agent = [sys.executable, "-I", str(BIN), "hold", "--pool", lane.pool,
             "--run", str(log.parent.parent.parent), "--log", str(log), "--",
             shutil.which("devin") or "devin", "--config", str(config), "--permission-mode", "accept-edits",
             "--respect-workspace-trust", "false", "acp", "--model", lane.model]
    # ACP collapses SWE-2 variants to swe-2-high plus thought_level. Passing
    # swe-2-max through acpx's --model rejects it or resets the native effort.
    argv = [shutil.which("acpx") or "acpx", "--agent", " ".join(_acpx_quote(a) for a in agent),
            "--cwd", str(work), "--deny-all", "--auth-policy", "skip", "--no-terminal", "--no-fs",
            "--prompt-retries", "0", "--format", "json", "exec", "--config-option", "mode=accept-edits", "--file", str(message)]
    return Launch(argv, None, env_set={**DEVIN_ENV, "DEVIN_MODEL": lane.model, "XCHECK_ROOT": str(ROOT)})

# Claude reviews register only local readers and the two keyless retrieval
# endpoints. --allowedTools pre-approves calls; it is not a tool allowlist.
# --strict-mcp-config excludes other registrations, and --disallowedTools
# removes account connectors even if the native client loads them separately.
# `mcp__*` is not an allow pattern Claude honours; each server is allowed by
# name, `mcp__<server>`, with non-identifier characters as `_`
# ("claude.ai Gmail" -> mcp__claude_ai_Gmail). bypassPermissions is refused
# in restricted mode.
CLAUDE_JSON = Path.home() / ".claude.json"
CLAUDE_LOCAL_MCP = {"semble", "fff", "qmd", "pond"}
CLAUDE_FREE_MCP = {
    "keenable-free": "https://api.keenable.ai/mcp",
    "you-free": "https://api.you.com/mcp?profile=free&tools=you-search",
}


def _claude_review_servers(web: bool) -> dict:
    try:
        data = json.loads(CLAUDE_JSON.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {name: server for name, server in (data.get("mcpServers") or {}).items()
            if isinstance(server, dict) and (name in CLAUDE_LOCAL_MCP or (
                web and name in CLAUDE_FREE_MCP and server.get("type") == "http"
                and server.get("url") == CLAUDE_FREE_MCP[name] and not server.get("headers")))}


def claude_mcp(web: bool = True) -> list[str]:
    # Pass only the selected MCP definitions, never the operator's full config.
    return ["--strict-mcp-config", "--mcp-config", json.dumps({"mcpServers": _claude_review_servers(web)}),
            "--disallowedTools", "mcp__claude_ai_*"]


def claude_mcp_allow(web: bool = True) -> list[str]:
    return sorted("mcp__" + name for name in _claude_review_servers(web))


CLAUDE_ENV = {"DISABLE_AUTOUPDATER": "1"}

# Agy runs through acpx and Antigravity's own ACP server, the headless form
# Antigravity ships for IDEs: `agy -p` ends the turn on any soft-denied tool and
# the app needs a committed ANSWER.md. The chain, outermost first:
#   acpx --no-terminal --no-fs   transport only; no client-side fs or shell
#   xcheck hold                  re-joins this run's pool (acpx passes only stdio)
#   sandbox-exec -f agy.sb       writes only to the workspace and lane home
#   agy_acp_server.par           Google's server; it logs in with agy's OAuth client
# The server's own terminal sandbox exists only for oauth-business admin
# controls, so the Seatbelt profile is the write boundary. Everything the server
# reads from GEMINI_HOME is the lane's: auth type, MCP list, no hooks; the login
# itself stays in the Keychain (service gemini, account antigravity-acp). Its
# temp files go to the lane home, and uvx-started MCP servers use a uv cache
# only Agy lanes share (ROOT/.cache/agy-uv), never the operator's.
# Kept current by agy-acp-server-update; XCHECK_AGY_ACP_DIR points tests at a fake.
AGY_ACP_DIR = Path(os.environ.get("XCHECK_AGY_ACP_DIR") or Path.home() / ".local/lib/antigravity-acp")
AGY_ACP_SERVER = AGY_ACP_DIR / "agy_acp_server.par"
AGY_LOGIN = ["security", "find-generic-password", "-s", "gemini", "-a", "antigravity-acp"]
ACPX_CONFIG = Path.home() / ".acpx" / "config.json"
# Keys in acpx's own config that would change a lane: a timeout cancels quiet
# reviewers, auth re-injects scrubbed credentials, MCP or agent entries change
# what runs, default permissions change what is approved.
ACPX_FORBIDDEN = ("timeout", "auth", "authPolicy", "mcpServers", "agents", "defaultPermissions",
                  "nonInteractivePermissions", "promptRetries")
# Review MCP servers the ACP server cannot start: fff rejects the `server/discover`
# probe the server sends before `initialize`, which fails the whole turn; qmd
# over HTTP never finishes connecting, and the server waits for every server.
AGY_MCP_EXCLUDED = {"fff", "qmd"}
AGY_LANE_ENV = {"TMPDIR", "UV_CACHE_DIR", "UV_TOOL_DIR"}  # set by the lane; never a server's own
BIN = Path(__file__).resolve().parent.parent / "bin" / "xcheck"


def _agy_launch(lane: Lane, work: Path, message: Path, log: Path, web: bool) -> Launch:
    from .files import ROOT  # files imports this module
    d = log.parent
    home = _agy_home(d / "gemini-home", web)
    uv = ROOT / ".cache" / "agy-uv"
    uv.mkdir(parents=True, exist_ok=True)
    profile = _agy_profile(d / "agy.sb", work, home, uv)
    agent = [sys.executable, "-I", str(BIN), "hold", "--pool", lane.pool, "--run", str(d.parent.parent),
             "--log", str(log), "--", "/usr/bin/sandbox-exec", "-f", str(profile), str(AGY_ACP_SERVER)]
    argv = [
        shutil.which("acpx") or "acpx", "--agent", " ".join(_acpx_quote(a) for a in agent),
        "--cwd", str(work), "--model", lane.model,
        # The server refuses every write and tool without it; --no-terminal and
        # --no-fs leave only the server's own tools, inside the Seatbelt profile.
        "--approve-all", "--no-terminal", "--no-fs", "--prompt-retries", "0",
        "--format", "json", "exec", "--file", str(message),
    ]
    return Launch(argv, None, env_set={"GEMINI_HOME": str(home), "XCHECK_ROOT": str(ROOT),
                                       "TMPDIR": str(home / "tmp"), "UV_CACHE_DIR": str(uv),
                                       "UV_TOOL_DIR": str(uv / "tools")})


def _acpx_quote(arg: str) -> str:
    """One argv word for acpx's --agent splitter (POSIX-style quotes)."""
    return arg if re.fullmatch(r"[A-Za-z0-9_@%+=:,./-]+", arg) else "'" + arg.replace("'", "'\"'\"'") + "'"


def _agy_home(home: Path, web: bool) -> Path:
    """The lane's GEMINI_HOME: auth type and review MCP servers only. The server
    reads HTTP servers from `url` and starts any entry with a `command`,
    `disabled` or not, so entries are written in that shape here."""
    (home / "antigravity-acp").mkdir(parents=True, exist_ok=True)
    (home / "config").mkdir(exist_ok=True)
    (home / "tmp").mkdir(exist_ok=True)
    (home / "antigravity-acp" / "settings.json").write_text(json.dumps({"auth": {"type": "oauth-personal"}}))
    servers = {}
    for name, server in _claude_review_servers(web).items():
        if name in AGY_MCP_EXCLUDED:
            continue
        if server.get("type") == "http":
            servers[name] = {"url": server["url"]}
        elif isinstance(server.get("command"), str):
            # The lane decides where a server may write; a server's own cache
            # or temp setting would point outside the Seatbelt profile.
            env = {k: v for k, v in (server.get("env") or {}).items() if k not in AGY_LANE_ENV}
            servers[name] = {"command": server["command"], "args": list(server.get("args") or []),
                             **({"env": env} if env else {})}
    (home / "config" / "mcp_config.json").write_text(json.dumps({"mcpServers": servers}, indent=1))
    return home


def _agy_profile(path: Path, work: Path, home: Path, uv: Path) -> Path:
    """Seatbelt: everything allowed except writes outside the workspace, the
    lane home, the Agy uv cache, devices and the login Keychain (the server
    rewrites its token there on every start)."""
    allowed = [os.path.realpath(p) for p in (work, home, uv)] + [
        "/dev", str(Path.home() / "Library" / "Keychains")]
    path.write_text("(version 1)\n(allow default)\n(deny file-write*)\n(allow file-write*\n"
                    # Seatbelt accepts UTF-8 paths, not JSON's \uXXXX escapes.
                    + "".join(f"  (subpath {json.dumps(p, ensure_ascii=False)})\n" for p in allowed) + ")\n")
    return path


def unavailable(lane: Lane) -> str | None:
    """Why this lane cannot start here, before admission; None when it can."""
    if lane.kind not in STREAM_ONLY:
        return None
    if not shutil.which("acpx"):
        return "startup-unavailable: acpx is not on PATH; install it with npm install -g acpx"
    if lane.kind == "agy" and not AGY_ACP_SERVER.is_file():
        return f"startup-unavailable: {AGY_ACP_SERVER} is missing; run agy-acp-server-update"
    try:
        config = json.loads(ACPX_CONFIG.read_text()) if ACPX_CONFIG.exists() else {}
    except (OSError, ValueError) as e:
        return f"startup-unavailable: {ACPX_CONFIG} is unreadable ({e}); fix or remove it"
    if not isinstance(config, dict):
        return f"startup-unavailable: {ACPX_CONFIG} must contain a JSON object; fix or remove it"
    if set_keys := [k for k in ACPX_FORBIDDEN if k in config]:
        return f"startup-unavailable: {ACPX_CONFIG} sets {', '.join(set_keys)}; xcheck lanes need acpx defaults"
    if lane.kind == "codex":
        return codex_acp_problem()
    if lane.kind in ("devin", "opencode"):
        return None if shutil.which(lane.kind) else f"startup-unavailable: {lane.kind} is not on PATH"
    if lane.kind == "grok":
        if not shutil.which("grok"):
            return "startup-unavailable: grok is not on PATH"
        if not GROK_AUTH_FILE.is_file():
            return ("auth-required: no Grok subscription login at ~/.grok/auth.json; "
                    "run `grok` once to sign in")
        return None
    login = subprocess.run(AGY_LOGIN, capture_output=True).returncode
    if login == 36:
        return "auth-required: the login keychain is locked; run keychain-unlock-gui at the screen"
    if login:
        return ("auth-required: no Antigravity ACP login in the Keychain (service gemini, account "
                "antigravity-acp); sign in once by running any acpx prompt against the server and "
                "opening the link it prints")
    return None

STREAM_ONLY = {"codex", "agy", "devin", "opencode", "grok"}


def view_for(kind: str, view: str) -> str:
    """The view a lane of `kind` actually runs in, given the run's view."""
    return "stream" if kind in STREAM_ONLY else view

# Terminal limit banners an app draws and then sits on, each seen on a real
# screen: Claude "You've hit your limit · resets 7pm (America/Los_Angeles)",
# tui.py counts them for diagnosis only.
# Quoted banners and silent work are indistinguishable from a stopped app
# without a native failure signal.
APP_LIMIT = re.compile(r"^\W*You(?:['’]ve)? hit your (?:weekly )?limit\b")


def tui(lane: Lane, work: Path, message: str, log: Path, web: bool) -> Launch:
    """The harness's interactive app, started with its initial prompt as an
    argument, so nothing is typed into it. The lane answers by writing
    ANSWER.md in `work`; the runner publishes it."""
    if lane.kind == "claude":
        tools = "Read,Write,WebSearch,WebFetch" if web else "Read,Write"
        argv = [
            "claude", "--restricted", *claude_mcp(web), "--permission-mode", "dontAsk",
            "--tools", tools, "--allowedTools", ",".join([tools, *claude_mcp_allow(web)]), "--add-dir", str(work),
            "--model", lane.model, "--effort", lane.effort, "--debug-file", str(log), message,
        ]
        return Launch(argv, None, env_set=CLAUDE_ENV)
    raise ValueError(f"unknown kind {lane.kind}")


# Flags each adapter relies on, for `xcheck doctor` (help-text drift check).
# Hidden-but-accepted flags are never used, so every entry must be listed.
HELP_COMMANDS = {
    "codex app-server": (["codex", "app-server", "--help"], ["--stdio"]),
    "claude": (["claude", "--help"], ["--print", "--restricted", "--mcp-config", "--permission-mode", "--tools", "--allowedTools", "--add-dir",
               "--no-session-persistence", "--model", "--effort", "--output-format", "--verbose",
               "--permission-prompts", "--include-partial-messages", "--debug-file",
               "--strict-mcp-config", "--disallowedTools"]),
    # grok itself only serves the startup probe (version --json); lanes run the agent.
    "grok": (["grok", "--help"], ["--sandbox", "--cwd"]),
    "grok agent": (["grok", "agent", "--help"], ["--model", "--reasoning-effort",
             "--always-approve", "--agent-profile", "--no-leader", "--debug-file"]),
    # agy itself only serves the capacity meter (`agy -p /usage`); lanes run acpx.
    "agy": (["agy", "--help"], ["--output-format", "--print-timeout"]),
    "acpx": (["acpx", "--help"], ["--agent", "--cwd", "--model", "--approve-all", "--no-terminal",
             "--no-fs", "--prompt-retries", "--format", "--deny-all", "--auth-policy"]),
    "acpx exec": (["acpx", "exec", "--help"], ["--file", "--config-option"]),
    "opencode acp": (["opencode", "acp", "--help"], ["--cwd"]),
    "opencode models": (["opencode", "models", "--help"], ["--verbose"]),
    "devin": (["devin", "--help"], ["--config", "--model", "--permission-mode",
               "--respect-workspace-trust"]),
    "devin acp": (["devin", "acp", "--help"], ["--model"]),
}


# --------------------------------------------------------------------------
# Stream reading. One Reader per lane consumes stream lines in order and knows
# the final text and any structured error when the stream ends.


@dataclass
class Reader:
    kind: str
    text: str = ""
    error: str | None = None
    _buf: list[str] = field(default_factory=list)
    completed: bool = False
    diagnostics: list[dict] = field(default_factory=list)
    _mcp_calls: dict[str, dict] = field(default_factory=dict)
    _prompt_id: object = None  # ACP: the JSON-RPC id of acpx's session/prompt
    _failure: str | None = None  # ACP: an error that ends the turn unless it completes anyway
    _client_requests: set = field(default_factory=set)  # ACP request ids are scoped to each direction

    def notes(self) -> list[str]:
        notes = []
        for d in self.diagnostics:
            if d["kind"] == "mcp_startup":
                notes.append(f"MCP startup {d['server']}: {d['status']}")
            else:
                outcome = d["outcome"]
                label = "POLICY VIOLATION" if outcome == "succeeded" else "MCP policy warning"
                notes.append(f"{label}: unapproved tool {d['tool']} {outcome}; "
                             f"{'execution unconfirmed; ' if outcome == 'attempted' else ''}billing unknown")
        return notes

    def feed(self, obj: dict) -> str | None:
        """Consume one stream object; return a short human line for the view."""
        return (self._acp if self.kind in STREAM_ONLY else getattr(self, f"_{self.kind}"))(obj)

    def finish(self, final_file: Path) -> None:
        if self.kind in STREAM_ONLY:
            self.text = "".join(self._buf)
            head = self.text.lstrip()
            # The server reports an MCP server that will not start as the whole
            # answer of a turn that still ends end_turn.
            if self.kind == "agy" and re.fullmatch(r"The MCP server '[^'\n]+' failed to initialize[^\n]*", head.strip()):
                self.error = _clip(head.splitlines()[0], 300)
            elif not self.completed and self._failure:
                self.error = self._failure
    # -- claude: stream-json; the `result` event carries the final text.
    def _claude(self, o: dict) -> str | None:
        t = o.get("type")
        if t == "system" and o.get("subtype") == "init":
            for server in o.get("mcp_servers") or []:
                if server.get("status") in ("failed", "needs-auth"):
                    self.diagnostics.append(dict(kind="mcp_startup", server=server.get("name"), status=server["status"]))
        if t == "user":
            for block in (o.get("message") or {}).get("content") or []:
                if block.get("type") == "tool_result" and block.get("tool_use_id") in self._mcp_calls:
                    self._mcp_calls[block["tool_use_id"]]["outcome"] = "error" if block.get("is_error") else "succeeded"
        if t == "result":
            self.completed = True
            # On is_error the `result` field is the error message, not an answer
            # (and `subtype` can still read "success").
            if o.get("is_error"):
                self.error = o.get("result") or json.dumps(o.get("errors")) or "is_error"
            else:
                self.text = o.get("result") or ""
            return None
        if t == "assistant":
            out = []
            for block in (o.get("message") or {}).get("content") or []:
                if block.get("type") == "tool_use":
                    name = block.get("name", "")
                    if name.startswith("mcp__") and name.split("__", 2)[1] not in CLAUDE_LOCAL_MCP | CLAUDE_FREE_MCP.keys():
                        diagnostic = dict(kind="mcp_policy", tool=name, outcome="attempted", billing="unknown")
                        self.diagnostics.append(diagnostic)
                        if isinstance(block.get("id"), str):
                            self._mcp_calls[block["id"]] = diagnostic
                    out.append(f"> {block.get('name')} {_clip(json.dumps(block.get('input')), 100)}")
                elif block.get("type") == "text":
                    out.append(_clip(block.get("text", "")))
            return "\n".join(out) or None
        return None

    # -- ACP lanes: acpx `--format json` is the raw ACP JSON-RPC stream in both
    # directions. Request ids are correlated both ways: only the response to
    # acpx's own session/prompt request ends the turn, and an RPC error
    # answering it is terminal. An error carried by acpx itself (id null) is
    # kept as the failure reason and decides only if no completion follows.
    # The answer is the text after the last tool call.
    def _acp(self, o: dict) -> str | None:
        # Requests in both directions share one id space, and an agent request
        # may reuse a client id (a real capture had session/request_permission
        # reuse the initialize id). Correlate before judging: every request
        # other than the client's own session/prompt is tracked, and a
        # response to a tracked request is consumed here — never read as the
        # prompt's terminal reply.
        if "method" in o and "id" in o:
            if o.get("method") == "session/prompt":
                self._prompt_id = o["id"]
            else:
                self._client_requests.add(o["id"])
            return None
        if o.get("id") in self._client_requests:
            self._client_requests.discard(o["id"])
            return None
        if (self.kind == "grok" and o.get("method") == "_x.ai/mcp/server_status"
                and (o.get("params") or {}).get("status") not in (None, "ready", "initialized")):
            p = o["params"]
            self.diagnostics.append(dict(kind="mcp_startup", server=p.get("name"),
                                         status=p.get("reason") or p.get("status")))
        update = (o.get("params") or {}).get("update") or {}
        kind = update.get("sessionUpdate")
        if kind == "agent_message_chunk":
            self._buf.append((update.get("content") or {}).get("text", ""))
            return None
        if kind == "tool_call":
            self._buf.clear()
            return f"> {_clip(update.get('title') or update.get('kind') or 'tool', 120)}"
        result = o.get("result")
        if (self._prompt_id is not None and isinstance(result, dict)
                and "stopReason" in result and o.get("id") == self._prompt_id):
            reason = result["stopReason"]
            if reason == "end_turn":
                self.completed = True
            else:
                self.error = f"{self.kind.title()} ended without completing the review: {reason}"
            return f"· end {reason}"
        error = o.get("error")
        if isinstance(error, dict) and o.get("id") in (None, self._prompt_id):
            detail = error.get("data")
            self._failure = _clip(str((detail.get("message") if isinstance(detail, dict) else detail)
                                     or error.get("message") or error), 300)
            # An error answering the client's own session/prompt is terminal:
            # no result can legally follow it. An id-null error is acpx's own
            # carry (agent disconnected, --model refused) and is kept as the
            # failure reason only if no completion follows.
            if self._prompt_id is not None and o.get("id") == self._prompt_id:
                self.error = self._failure
            return f"! {self._failure}"
        return None

def _clip(s: str, n: int = 160) -> str:
    s = re.sub(r"\s+", " ", s or "").strip()
    return s if len(s) <= n else s[: n - 1] + "…"
