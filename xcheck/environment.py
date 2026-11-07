"""Peer environment policy, independent of launch construction and execution.

Native authentication stays canonical. A launch can restore only its deliberate
environment additions after inherited orchestrator routing and keys are removed.
XCHECK_LANE also tells the operator's shell startup to skip loading secrets.
"""
import os
from pathlib import Path

# CLAUDE* includes identity, effort, config homes and adapter routing. Keeping
# ANTHROPIC_AUTH_TOKEN/BASE_URL can silently replace subscription auth with an
# API route. ACPX_AUTH_* is promoted by acpx into the agent environment.
SCRUB_PREFIXES = ("HERDR_", "CLAUDE", "CODEX_COMPANION_", "CODEX_SANDBOX",
                  "CLINE_", "OPENCODE", "ACPX_", "CODEX_ACP_", "XCHECK_CODEX_",
                  "ANTHROPIC_", "AGENT_PROXY_", "CCR_")
SCRUB_KIND = {"grok": ("GROK_",)}
NO_HERDR = "/nonexistent/xcheck-lanes-have-no-herdr.sock"
SCRUB_NAMES = {
    "OLDPWD", "CREDENTIALS_DIRECTORY", "MAX_THINKING_TOKENS", "IS_SANDBOX",
    "CODEX_PATH", "CODEX_CONFIG", "INITIAL_AGENT_MODE", "DEFAULT_AUTH_REQUEST",
    "MODEL_PROVIDER", "APP_SERVER_LOGS", "NO_BROWSER", "CODEX_SESSION_ID", "CODEX_THREAD_ID",
    "CLOUD_ML_REGION", "GOOGLE_CLOUD_PROJECT", "GROK_SANDBOX", "GROK_HOME",
    "GROK_AUTH_PATH", "GROK_CONFIG", "GROK_CONFIG_DIR", "GROK_MANAGED_CONFIG_DIR",
    "GROK_AGENT_PROFILE", "GROK_MODEL", "GROK_REASONING_EFFORT", "GROK_TOOLS",
}
# Grok rebuilds its full environment; other peers retain harmless GROK_* prefs
# but not redirecting knobs that their nested shell commands could inherit.
# Region/project selectors stay out even with their paid-routing switches off.


def lane_env(spec, work: Path) -> dict[str, str]:
    prefixes = SCRUB_PREFIXES + SCRUB_KIND.get(spec.kind, ())
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(prefixes) and k not in SCRUB_NAMES
           and not k.endswith("_API_KEY")}
    # OpenCode roots tools at PWD; inherited PWD would target the orchestrator.
    env["PWD"] = str(work)
    # Removing HERDR_* alone leaves the CLI's default socket reachable.
    env["HERDR_SOCKET_PATH"] = NO_HERDR
    env["XCHECK_LANE"] = "1"
    env.update(spec.env_set)
    return env
