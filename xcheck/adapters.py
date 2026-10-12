"""Per-harness knowledge: how to start a lane, and how to read what it streams.

Codex and Claude run their first-party headless streams (`codex exec --json`,
`claude -p --output-format stream-json`); every other lane is an acpx (ACP
client) exec against the harness's ACP server. The
vendor-specific part is the command, its environment and its native policy. Every flag here was checked against CLI help and smoke runs, and
`xcheck doctor` re-checks flags against installed help.

Lanes print their answer as their final message; the one Reader below
extracts it from the lane's stream (native for Codex and Claude, ACP for the
rest) and decides native completion. Grok,
OpenCode and Devin may write scratch files in their workspace while reviewing.
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
        if self.kind == "opencode":
            # OpenCode has no quota meter, so a pool only serializes: one run per
            # Zen model (different free models run side by side), and one run per
            # other provider, whose models share that account's limits.
            provider, _, model = self.model.partition("/") if "/" in self.model else ("opencode", "", self.model)
            return f"opencode-zen-{model}" if provider == "opencode" else f"opencode-{provider}"
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
    # Free OpenCode Zen pins, each qualified by a real review run. Data terms
    # are the provider's (opencode.ai/console/docs/models, Privacy); mind what a packet holds.
    Lane("opencode", "opencode/space-bunny-free"),  # zero retention, no training
    Lane("opencode", "opencode/step-5-preview-free"),  # zero retention, no training
    Lane("opencode", "opencode/mimo-v2.6-flash-free"),  # may be used to improve the model
    Lane("opencode", "opencode/muse-spark-1.3-contributor-free"),  # Contributor: may log or train
    # Free OpenRouter pin, qualified by a real review run; served by NVIDIA itself.
    Lane("opencode", "openrouter/nvidia/nemotron-3-ultra-550b-a55b:free"),  # NVIDIA trial: logged; no personal or confidential data
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


def launch(lane: Lane, work: Path, message: Path, log: Path, web: bool) -> Launch:
    """The command for one lane. `work` is its workspace, `log` its forensic
    log."""
    if lane.kind == "codex":
        return _codex_launch(lane, work, message, log, web)
    if lane.kind == "claude":
        return _claude_launch(lane, work, message, log, web)
    if lane.kind == "grok":
        return _grok_launch(lane, work, message, log, web)
    if lane.kind == "agy":
        return _agy_launch(lane, work, message, log, web)
    if lane.kind == "devin":
        return _devin_launch(lane, work, message, log, web)
    if lane.kind == "opencode":
        return _opencode_launch(lane, work, message, log)
    raise ValueError(f"unknown kind {lane.kind}")


# Codex features a review never needs: plugins bring their own MCP servers
# (a computer-use REPL among them), hooks can block a lane, apps and browser
# or computer use act outside the packet.
CODEX_DISABLED_FEATURES = ("plugins", "hooks", "apps", "computer_use", "browser_use")


def _codex_review_mcp(server: dict, web: bool) -> bool:
    """One entry of `codex mcp list --json` that a review may keep: an approved
    name in a keyless shape (the same rule as Claude's registrations) with no
    stored login, which the shape cannot show (Codex keeps MCP OAuth tokens)."""
    name, transport = server.get("name"), server.get("transport")
    if not isinstance(name, str) or not isinstance(transport, dict) \
            or server.get("auth_status") not in ("unsupported", "not_logged_in"):
        return False
    if transport.get("type") == "stdio":
        env = transport.get("env") or {}
        return (name in REVIEW_LOCAL_MCP and isinstance(env, dict) and set(env) <= LOCAL_MCP_ENV
                and not transport.get("env_vars"))
    if transport.get("type") != "streamable_http" or any(
            transport.get(k) for k in ("bearer_token_env_var", "http_headers", "env_http_headers", "http_headers_helper")):
        return False
    url = transport.get("url")
    return isinstance(url, str) and (
        (name in REVIEW_LOCAL_MCP and LOOPBACK_URL.fullmatch(url) is not None)
        or (web and url == REVIEW_FREE_MCP.get(name)))


def _codex_mcp_enabled(features: list[str], overrides: list[str], env: dict, work: Path) -> list[dict]:
    out = subprocess.run([shutil.which("codex") or "codex", *features, *overrides, "mcp", "list", "--json"],
                         env=env, cwd=work, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)
    servers = json.loads(out.stdout) if out.returncode == 0 else None
    if not isinstance(servers, list):
        raise ValueError(f"`codex mcp list --json` failed (exit {out.returncode})")
    # Anything not explicitly disabled counts as enabled, so a changed output
    # shape makes the launch refuse servers instead of overlooking them.
    return [s for s in servers if not (isinstance(s, dict) and s.get("enabled") is False)]


def _codex_launch(lane: Lane, work: Path, message: Path, log: Path, web: bool) -> Launch:
    """Codex's own headless form: `codex exec --json`, the message on stdin.
    `--ephemeral` keeps no session files and `--sandbox read-only` is the
    native sandbox (exec never asks for approval). MCP follows the review
    rule: Codex's effective server list is read zero-turn, every server that
    is not an approved keyless one is disabled by name, and the list is read
    again to prove it; a lane whose list cannot be proved does not start.
    The installed binary is the runner's direct child and holds the pool
    itself. The ACP form of this lane (codex-acp) is kept on the `acp/codex`
    branch."""
    from .lane import lane_env
    features = [arg for f in CODEX_DISABLED_FEATURES for arg in ("--disable", f)]
    env = lane_env(Launch([], message), work)
    try:
        enabled = _codex_mcp_enabled(features, [], env, work)
        refused = sorted(str(s.get("name") if isinstance(s, dict) else s) for s in enabled
                         if not _codex_review_mcp(s if isinstance(s, dict) else {}, web))
        if bad := [n for n in refused if not re.fullmatch(r"[A-Za-z0-9_-]+", n)]:
            raise ValueError(f"cannot disable MCP server(s) {bad} by name")
        overrides = [arg for n in refused for arg in ("--config", f"mcp_servers.{n}.enabled=false")]
        left = [s.get("name") if isinstance(s, dict) else s for s in _codex_mcp_enabled(features, overrides, env, work)
                if not _codex_review_mcp(s if isinstance(s, dict) else {}, web)]
    except (OSError, subprocess.SubprocessError, ValueError) as e:
        raise Unavailable(f"startup-unavailable: Codex MCP selection could not be proved ({_clip(str(e), 200)})") from e
    if left:
        raise Unavailable(f"startup-unavailable: Codex still enables unapproved MCP server(s) {left} after disabling them")
    argv = [shutil.which("codex") or "codex", *features, "exec", "--skip-git-repo-check", "--ephemeral",
            "--sandbox", "read-only", "--cd", str(work), "--model", lane.model,
            "--config", f'model_reasoning_effort="{lane.effort}"',
            "--config", f'web_search="{"live" if web else "disabled"}"',
            "--config", "check_for_update_on_startup=false", *overrides, "--json", "-"]
    return Launch(argv, message)


def _opencode_model(lane: Lane) -> list[str]:
    model = lane.model if "/" in lane.model else f"opencode/{lane.model}"
    return ["--model", model]


def _opencode_launch(lane: Lane, work: Path, message: Path, log: Path) -> Launch:
    from .files import ROOT
    agent = [sys.executable, "-I", str(BIN), "hold", "--pool", lane.pool,
             "--run", str(log.parent.parent.parent), "--log", str(log), "--",
             shutil.which("opencode") or "opencode", "acp"]
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
            metadata = client_metadata(env=lane_env(spec, work), cwd=work)
        except (OSError, subprocess.SubprocessError, ValueError) as e:
            raise Unavailable(f"startup-unavailable: `opencode api model.list --standalone` failed ({_clip(str(e), 200)})") from e
        if model not in metadata:
            raise Unavailable(f"startup-unavailable: {model} is not in `opencode api model.list --standalone`; "
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
GROK_VERIFIED_VERSION = "1.0.52 (0920c8169e27)"
GROK_ENV = {
    # A subscription session, never a paid API-key fallback (XAI_API_KEY is
    # scrubbed anyway; this refuses to use it if one ever leaked in).
    "GROK_DISABLE_API_KEY_AUTH": "1",
    "GROK_DISABLE_AUTOUPDATER": "1", "GROK_TELEMETRY_ENABLED": "0",
    # No delegation, memory, title refresh or other side sampling. A fresh
    # session still samples one initial title: no supported switch disables
    # it, and a pre-prompt _x.ai/session/rename only discards its result (see
    # integrations/grok-acp/README.md). usage.json's modelCalls excludes it.
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
REVIEW_LOCAL_MCP = {"semble", "fff", "qmd", "pond"}
REVIEW_FREE_MCP = {
    "keenable-free": "https://api.keenable.ai/mcp",
    "you-free": "https://api.you.com/mcp?profile=free&tools=you-search",
}
# A trusted local name is a trust decision about the command, not proof that
# its registration is keyless: an approved entry may carry only these cache
# locations in its env, and an HTTP one must be loopback without headers.
# Anything else (a key, a token, a header helper, a remote URL) drops the
# server instead of forwarding a credential channel past the lane's key scrub.
LOCAL_MCP_ENV = {"UV_CACHE_DIR", "UV_TOOL_DIR", "NPM_CONFIG_CACHE"}
LOOPBACK_URL = re.compile(r"http://(?:127\.0\.0\.1|localhost|\[::1\])(?::\d+)?(?:/[^\s?#@]*)?")


def _local_mcp_ok(server: dict) -> bool:
    if server.get("type", "stdio") == "stdio":
        args, env = server.get("args", []), server.get("env", {})
        return (set(server) <= {"type", "command", "args", "env"} and isinstance(server.get("command"), str)
                and isinstance(args, list) and all(isinstance(a, str) for a in args)
                and isinstance(env, dict) and set(env) <= LOCAL_MCP_ENV
                and all(isinstance(v, str) for v in env.values()))
    return (server.get("type") == "http" and set(server) == {"type", "url"}
            and isinstance(server["url"], str) and LOOPBACK_URL.fullmatch(server["url"]) is not None)


def _claude_review_servers(web: bool) -> dict:
    """The operator's registrations a review may use: approved local readers in
    the keyless shape above, plus (live web only) the exact keyless endpoints."""
    try:
        data = json.loads(CLAUDE_JSON.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    servers = data.get("mcpServers")
    return {name: server for name, server in (servers.items() if isinstance(servers, dict) else ())
            if isinstance(server, dict) and (
                (name in REVIEW_LOCAL_MCP and _local_mcp_ok(server))
                or (web and name in REVIEW_FREE_MCP and server == {"type": "http", "url": REVIEW_FREE_MCP[name]}))}


# Native flags the lane depends on; admission and doctor require each in
# `claude --help`, since the operator CLI upgrades on its own schedule.
CLAUDE_NATIVE_FLAGS = ("--print", "--restricted", "--strict-mcp-config", "--mcp-config", "--tools",
                       "--allowedTools", "--disallowedTools", "--permission-mode", "--permission-prompts",
                       "--add-dir", "--no-session-persistence", "--model", "--effort", "--debug-file",
                       "--output-format", "--verbose", "--include-partial-messages")


def _claude_launch(lane: Lane, work: Path, message: Path, log: Path, web: bool) -> Launch:
    """Claude Code's own headless stream: `claude -p --output-format
    stream-json`, the message on stdin. `--restricted` ignores settings files
    and hooks and confines file tools to the working directories;
    `--strict-mcp-config` with an inline `--mcp-config` registers only the
    validated review servers; `--tools` lists the built-ins and
    `--allowedTools` pre-approves them and each server by name; account
    connectors are disallowed. The installed binary is the runner's direct
    child and holds the pool itself. The ACP form of this lane
    (xcheck-acpx + claude-agent-acp) is kept on the `acp/claude` branch."""
    servers = _claude_review_servers(web)
    tools = ["Read", *(["WebSearch", "WebFetch"] if web else [])]
    argv = [shutil.which("claude") or "claude", "-p", "--restricted",
            "--strict-mcp-config", "--mcp-config", json.dumps({"mcpServers": servers}),
            "--disallowedTools", "mcp__claude_ai_*",
            # With -p, anything that would prompt is denied instead of waiting.
            "--permission-mode", "dontAsk", "--permission-prompts", "none",
            "--tools", ",".join(tools), "--allowedTools", ",".join([*tools, *sorted(f"mcp__{n}" for n in servers)]),
            "--add-dir", str(work), "--no-session-persistence", "--model", lane.model, "--effort", lane.effort,
            "--debug-file", str(log),
            # Partial messages show native output during a long think.
            "--output-format", "stream-json", "--verbose", "--include-partial-messages"]
    return Launch(argv, message, env_set={"DISABLE_AUTOUPDATER": "1"})


def claude_problem() -> str | None:
    """A subscription lane must prove the claude.ai login in the scrubbed lane
    environment, and the installed CLI must still list the flags it relies on."""
    from .lane import lane_env
    env = lane_env(Launch([], None), Path.cwd())
    claude = shutil.which("claude")
    if not claude:
        return "startup-unavailable: claude is not on PATH"
    try:
        out = subprocess.run([claude, "auth", "status", "--json"],
                             env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10)
        auth = json.loads(out.stdout) if out.returncode == 0 else {}
    except (OSError, subprocess.SubprocessError, ValueError):
        auth = {}
    if not (isinstance(auth, dict) and auth.get("loggedIn") and auth.get("authMethod") == "claude.ai"
            and auth.get("apiProvider") == "firstParty"):
        return ("auth-required: `claude auth status` is not a first-party claude.ai subscription login "
                "(scrubbed routing overrides expose a different source, or the Keychain login is missing); "
                "run claude once to refresh it")
    try:
        help_text = subprocess.run([claude, "--help"], env=env, stdin=subprocess.DEVNULL,
                                   capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        help_text = ""
    if missing := [f for f in CLAUDE_NATIVE_FLAGS if f not in help_text]:
        return (f"startup-unavailable: the installed claude no longer lists {', '.join(missing)}; "
                "the lane's native limits depend on them — re-verify the lane before running it")
    return None

# Agy runs through acpx and Antigravity's own ACP server, the headless form
# Antigravity ships for IDEs: `agy -p` ends the turn on any soft-denied tool.
# The chain, outermost first:
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
    if lane.kind == "codex":
        return None if shutil.which("codex") else "startup-unavailable: codex is not on PATH"
    if lane.kind == "claude":
        return claude_problem()
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

# Flags each adapter relies on, for `xcheck doctor` (help-text drift check).
# Hidden-but-accepted flags are never used, so every entry must be listed.
HELP_COMMANDS = {
    "codex exec": (["codex", "exec", "--help"], ["--skip-git-repo-check", "--ephemeral", "--sandbox",
                   "--cd", "--model", "--config", "--json", "--disable"]),
    "codex mcp list": (["codex", "mcp", "list", "--help"], ["--json", "--config", "--disable"]),
    # The capacity meter reads account/rateLimits/read from the app server.
    "codex app-server": (["codex", "app-server", "--help"], ["--stdio"]),
    # Admission checks these too, since the CLI upgrades separately.
    "claude": (["claude", "--help"], list(CLAUDE_NATIVE_FLAGS)),
    "claude auth status": (["claude", "auth", "status", "--help"], ["--json"]),
    # grok itself only serves the startup probe (version --json); lanes run the agent.
    "grok": (["grok", "--help"], ["--sandbox", "--cwd"]),
    "grok agent": (["grok", "agent", "--help"], ["--model", "--reasoning-effort",
             "--always-approve", "--agent-profile", "--no-leader", "--debug-file"]),
    # agy itself only serves the capacity meter (`agy -p /usage`); lanes run acpx.
    "agy": (["agy", "--help"], ["--output-format", "--print-timeout"]),
    "acpx": (["acpx", "--help"], ["--agent", "--cwd", "--model", "--approve-all", "--no-terminal",
             "--no-fs", "--prompt-retries", "--format", "--deny-all", "--auth-policy"]),
    "acpx exec": (["acpx", "exec", "--help"], ["--file", "--config-option"]),
    "opencode acp": (["opencode", "acp", "--help"], []),
    "opencode api": (["opencode", "api", "--help"], ["--standalone"]),
    "opencode models": (["opencode", "models", "--help"], ["--standalone"]),
    "devin": (["devin", "--help"], ["--config", "--model", "--permission-mode",
               "--respect-workspace-trust"]),
    "devin acp": (["devin", "acp", "--help"], ["--model"]),
}


# --------------------------------------------------------------------------
# Stream reading. One Reader per lane consumes stream lines in order and knows
# the final text and any structured error when the stream ends.


def _id_key(value) -> str:
    """JSON-RPC ids compare by JSON value: 1 and "1" are different requests."""
    return json.dumps(value)


def _error_text(error: dict) -> str:
    detail = error.get("data")
    return _clip(str((detail.get("message") if isinstance(detail, dict) else detail)
                     or error.get("message") or error), 300)


@dataclass
class Reader:
    """One lane's stream. Codex and Claude read their native streams (the
    `_codex` / `_claude` methods); every other kind reads acpx `--format json`,
    the raw ACP JSON-RPC traffic in both directions, printed without its
    direction.

    JSON-RPC ids belong to the side that sent the request, so an agent request
    can reuse the id of acpx's own session/prompt. Correlation therefore never
    assumes one id space:
    - only a session/prompt response carries `stopReason` (ACP schema), so that
      shape alone settles the turn, whichever id it reuses;
    - every other request id, from either side, is counted until answered, and
      a sender reuses an id only once its earlier request at it is answered;
    - an error carrying the prompt's id while another request with that id is
      open is ambiguous: it is held until the other response at that id
      resolves which side sent it, or until acpx — the client, which does know
      the direction — exits without a completion.
    Errors that answer other requests, and acpx's own id-null errors, are kept
    only as the failure reason if the turn never completes.
    The answer is the agent text after the last tool call."""
    kind: str
    web: bool = True  # the run's mode: the MCP audit approves what the launch approved
    text: str = ""
    error: str | None = None
    completed: bool = False
    diagnostics: list[dict] = field(default_factory=list)
    _buf: list[str] = field(default_factory=list)
    _mcp_calls: dict[str, dict] = field(default_factory=dict)  # Claude: tool_use id → policy diagnostic
    _prompt_id: object = None
    _open: dict[str, int] = field(default_factory=dict)  # unanswered non-prompt requests per id
    _held: str | None = None  # an ambiguous error at the prompt id
    _failure: str | None = None  # the reason to report if the turn never completes

    @classmethod
    def replay(cls, kind: str, stream: Path, web: bool) -> "Reader":
        """Rebuild a lane's reader from its saved stream (recovery after a
        runner died). Completion stays exactly what the live reader requires."""
        reader = cls(kind, web)
        try:
            lines = stream.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            lines = []
        for line in lines:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict):
                reader.feed(event)
        reader.finish()
        return reader

    def notes(self) -> list[str]:
        notes = []
        for d in self.diagnostics:
            if d["kind"] == "mcp_startup":
                notes.append(f"MCP startup {d['server']}: {d['status']}")
            elif d["kind"] == "auth":
                notes.append(f"auth source: {d['label']}")
            else:
                outcome = d["outcome"]
                label = "POLICY VIOLATION" if outcome == "succeeded" else "MCP policy warning"
                notes.append(f"{label}: unapproved tool {d['tool']} {outcome}; "
                             f"{'execution unconfirmed; ' if outcome == 'attempted' else ''}billing unknown")
        return notes

    def native(self):
        """The kind's own stream reader, for a harness whose first-party
        headless stream xcheck reads directly; None means ACP."""
        return getattr(self, f"_{self.kind}", None)

    def finish(self) -> None:
        if self.native():
            if not self.completed and not self.error and self._failure:
                self.error = self._failure
            return
        self.text = "".join(self._buf)
        head = self.text.strip()
        # Agy's server reports an MCP server that will not start as the whole
        # answer of a turn that still ends end_turn.
        if self.kind == "agy" and re.fullmatch(r"The MCP server '[^'\n]+' failed to initialize[^\n]*", head):
            self.error = _clip(head.splitlines()[0], 300)
        elif not self.completed and not self.error and (self._held or self._failure):
            self.error = self._held or self._failure

    def feed(self, o: dict) -> str | None:
        """Consume one stream object; return a short human line for the view."""
        if native := self.native():
            return native(o)
        if "method" in o:
            if "id" in o:  # a request, from either side
                if o["method"] == "session/prompt" and self._prompt_id is None:
                    # acpx sends one prompt (--prompt-retries 0); session/prompt
                    # is client-to-agent, so a later one is an ordinary request.
                    self._prompt_id = o["id"]
                else:
                    key = _id_key(o["id"])
                    if self._held is not None and self._open.get(key) and o["id"] == self._prompt_id:
                        # A sender reuses an id only once its earlier request at
                        # that id is answered, and the prompt is still open, so
                        # the held error answered that earlier request.
                        self._open[key] -= 1
                        self._held = None
                    self._open[key] = self._open.get(key, 0) + 1
                return None
            return self._notification(o)
        if "result" in o or "error" in o:
            return self._response(o)
        return None

    def _response(self, o: dict) -> str | None:
        rid, result, error = o.get("id"), o.get("result"), o.get("error")
        if isinstance(result, dict) and "stopReason" in result:
            if self._prompt_id is None or rid != self._prompt_id:
                return None
            self._held = None  # an error held at this id answered the other request
            reason = result["stopReason"]
            if reason == "end_turn":
                self.completed = True
            else:
                self.error = f"{self.kind.title()} ended without completing the review: {reason}"
            return f"· end {reason}"
        key = _id_key(rid)
        is_prompt = self._prompt_id is not None and rid == self._prompt_id
        if not isinstance(error, dict):
            if self._open.get(key):
                self._open[key] -= 1
                if is_prompt and self._held:
                    # The other request at this id is answered: the held error was the prompt's.
                    self.error, self._held = self._held, None
                    return f"! {self.error}"
            return None
        message = _error_text(error)
        if is_prompt and self._open.get(key):
            if self._held is None:
                self._held = message
                return f"! {message}"
            # Both responses at this id are errors; one of them ended the prompt.
            self._open[key] -= 1
            self.error, self._held = f"{self._held}; {message}", None
            return f"! {message}"
        if is_prompt:
            # The prompt's own failure: no result can legally follow it.
            self.error = self._failure = message
            return f"! {message}"
        if self._open.get(key):
            self._open[key] -= 1
        # acpx's own id-null error (agent disconnected, --model refused) or an
        # error answering another request: the reason, unless the turn completes.
        self._failure = message
        return f"! {message}"

    def _notification(self, o: dict) -> str | None:
        method, params = o.get("method"), o.get("params")
        params = params if isinstance(params, dict) else {}  # a malformed event is ignored, never fatal
        if (self.kind == "grok" and method == "_x.ai/mcp/server_status"
                and params.get("status") not in (None, "ready", "initialized")):
            self.diagnostics.append(dict(kind="mcp_startup", server=params.get("name"),
                                         status=params.get("reason") or params.get("status")))
            return None
        update = params.get("update")
        update = update if isinstance(update, dict) else {}
        kind = update.get("sessionUpdate")
        if kind == "agent_message_chunk":
            content = update.get("content")
            text = content.get("text") if isinstance(content, dict) else None
            self._buf.append(text if isinstance(text, str) else "")
            return None
        if kind == "tool_call":
            self._buf.clear()
            return f"> {_clip(update.get('title') or update.get('kind') or 'tool', 120)}"
        return None

    # -- codex: `codex exec --json`. Each agent_message is a whole message, so
    # the last one is the answer; turn.completed ends the turn and turn.failed
    # fails it. A top-level `error` (a reconnect notice) can recover, so it is
    # only the reason if the turn never completes.
    def _codex(self, o: dict) -> str | None:
        t = o.get("type")
        item = o.get("item") if isinstance(o.get("item"), dict) else {}
        if t == "turn.completed":
            self.completed = True
            return "· end turn"
        if t == "turn.failed":
            error = o.get("error")
            self.error = _clip(str((error.get("message") if isinstance(error, dict) else None)
                                   or self._failure or "turn.failed"), 300)
            return f"! turn failed: {self.error}"
        if t == "error":
            self._failure = _clip(str(o.get("message") or "error"), 300)
            return f"! {self._failure}"
        if item.get("type") == "mcp_tool_call" and t in ("item.started", "item.completed"):
            self._codex_mcp_policy(item, t)
        if t == "item.started" and item.get("type") not in (None, "agent_message", "reasoning"):
            self.text = ""  # narration before a tool call is not the answer
            if item["type"] == "command_execution":
                return f"$ {_clip(str(item.get('command', '')), 120)}"
            return f"> {item['type']}"
        if t == "item.completed" and item.get("type") == "agent_message":
            self.text = item.get("text") if isinstance(item.get("text"), str) else ""
            return None
        if t == "item.completed" and isinstance(item.get("type"), str):
            return f"> {item['type']}"
        return None

    def _approved_mcp(self) -> set[str]:
        return REVIEW_LOCAL_MCP | (REVIEW_FREE_MCP.keys() if self.web else set())

    def _codex_mcp_policy(self, item: dict, event: str) -> None:
        """A call to an MCP server the launch did not approve (it should have
        been disabled): one diagnostic per item id, updated on completion."""
        server = item.get("server")
        if not isinstance(server, str) or server in self._approved_mcp():
            return
        call = item.get("id") if isinstance(item.get("id"), str) else None
        diagnostic = self._mcp_calls.get(call) if call else None
        if diagnostic is None:
            diagnostic = dict(kind="mcp_policy", tool=f"mcp__{server}__{item.get('tool')}", outcome="attempted",
                              billing="unknown")
            self.diagnostics.append(diagnostic)
            if call:
                self._mcp_calls[call] = diagnostic
        if event == "item.completed":
            diagnostic["outcome"] = "error" if item.get("status") == "failed" or item.get("error") else "succeeded"

    # -- claude: `claude -p --output-format stream-json`. The `result` event
    # ends the turn and carries the final text; with is_error it is the error
    # (even when subtype says success). init lists each MCP server's startup
    # status. Unapproved MCP calls are audited per tool_use id: a repeated id
    # is one call, a later call to the same server is another, and its
    # tool_result records whether it succeeded.
    def _claude(self, o: dict) -> str | None:
        t = o.get("type")
        message = o.get("message") if isinstance(o.get("message"), dict) else {}
        blocks = [b for b in (message.get("content") or []) if isinstance(b, dict)] \
            if isinstance(message.get("content"), list) else []
        if t == "system" and o.get("subtype") == "init":
            for server in o.get("mcp_servers") if isinstance(o.get("mcp_servers"), list) else ():
                if isinstance(server, dict) and server.get("status") in ("failed", "needs-auth"):
                    self.diagnostics.append(dict(kind="mcp_startup", server=server.get("name"), status=server["status"]))
            return None
        if t == "result":
            if o.get("is_error"):
                self.error = _clip(str(o.get("result") or json.dumps(o.get("errors")) or "is_error"), 300)
                return f"! {self.error}"
            self.completed = True
            self.text = o.get("result") if isinstance(o.get("result"), str) else ""
            return "· end turn"
        if t == "user":
            for block in blocks:
                call = self._mcp_calls.get(block.get("tool_use_id")) if block.get("type") == "tool_result" else None
                if call:
                    call["outcome"] = "error" if block.get("is_error") else "succeeded"
            return None
        if t == "assistant":
            out = []
            for block in blocks:
                if block.get("type") == "tool_use":
                    self._claude_mcp_policy(block)
                    out.append(f"> {block.get('name')} {_clip(json.dumps(block.get('input')), 100)}")
                elif block.get("type") == "text":
                    out.append(_clip(str(block.get("text", ""))))
            return "\n".join(out) or None
        return None

    def _claude_mcp_policy(self, block: dict) -> None:
        """One diagnostic per unapproved MCP tool_use; the exact
        `mcp__<server>__<tool>` name, never text that merely quotes one."""
        call, name = block.get("id"), block.get("name")
        if not isinstance(name, str) or (isinstance(call, str) and call in self._mcp_calls):
            return
        parts = name.split("__")
        if len(parts) < 3 or parts[0] != "mcp" or parts[1] in self._approved_mcp():
            return
        diagnostic = dict(kind="mcp_policy", tool=name, outcome="attempted", billing="unknown")
        self.diagnostics.append(diagnostic)
        if isinstance(call, str):
            self._mcp_calls[call] = diagnostic


def _clip(s: str, n: int = 160) -> str:
    s = re.sub(r"\s+", " ", s or "").strip()
    return s if len(s) <= n else s[: n - 1] + "…"
