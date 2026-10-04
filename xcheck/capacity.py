"""May this lane spend from its quota pool now?

Each pool has one free meter that runs no inference:

  codex   `codex app-server` JSON-RPC `account/rateLimits/read`  (5 h and weekly windows)
  claude  Anthropic OAuth usage endpoint, token from the login keychain
  grok    Grok CLI billing, credits view (the TUI's "weekly limit left")
  agy     `agy -p /usage --output-format json` (a client-side command: zero turns)
  opencode opt-in (no protocol meter)
  devin   opt-in (no protocol meter)

A lane spends only when every window it draws on has more than the reserve
left: 10 %, or 5 + 5n % for the n lanes of one run sharing an Agy family
(capped at 90). A meter that cannot be read is `capacity-unknown`, a login the
provider rejects is `auth-required`; both skip the lane. There is no fallback
meter: a reading is trusted or the lane does not spend.
"""

from __future__ import annotations

import json
import math
import os
import re
import select
import signal
import subprocess
import time
import urllib.error
import urllib.request
from email.utils import parsedate_to_datetime
from dataclasses import dataclass
from pathlib import Path

from .adapters import Lane

RESERVE = 10.0
TIMEOUT = 20.0     # per network call or local command
AGY_TIMEOUT = 90.0  # `agy -p /usage` may have to start Agy's language server first
CLAUDE_LOGIN = "env -u ANTHROPIC_API_KEY claude auth login --claudeai"
# A locked keychain (security exit 36, e.g. over SSH) hides the Claude token.
# The operator can pass a fresh reading from Claude Code's /usage instead.
CLAUDE_MANUAL = ("XCHECK_CLAUDE_REMAINING", "XCHECK_CLAUDE_REMAINING_AT")
CLAUDE_MANUAL_AGE = 300
CLAUDE_THROTTLE_WAIT = 30.0  # total seconds 429s may delay one reading before it fails closed
CLAUDE_EXPIRY_MARGIN = 60.0  # probe budget plus modest clock skew; never spend on near-expiry auth
# `-p /usage` is a client-side command only from this release on; an older CLI
# would send it to a model as a prompt.
AGY_USAGE_SINCE = (1, 1, 11)


@dataclass(frozen=True)
class Reading:
    status: str  # eligible | reserve-low | auth-required | capacity-unknown
    note: str

    @property
    def ok(self) -> bool:
        return self.status == "eligible"


class LoginRequired(Exception):
    pass


def sharing(lane: Lane, roster) -> int:
    """Lanes of one roster drawing on this lane's pool (the Agy reserve grows with it)."""
    return sum(1 for other in roster if other.pool == lane.pool)


def reserve_for(lane: Lane, same_pool: int) -> float:
    return min(90.0, 5.0 + 5.0 * same_pool) if lane.kind == "agy" else RESERVE


def read(lane: Lane, same_pool: int = 1) -> Reading:
    """The lane's pool reading against its reserve. Never raises."""
    if lane.kind in ("opencode", "devin"):
        return Reading("eligible", f"{lane.kind} opt-in: no protocol meter; a quota error is the lane's error")
    reserve = reserve_for(lane, same_pool)
    try:
        windows = METERS[lane.kind](lane)
    except LoginRequired as e:
        return Reading("auth-required", str(e))
    except Exception as e:  # any unreadable meter fails closed
        return Reading("capacity-unknown", f"{lane.kind} meter unreadable: {type(e).__name__}: {e}")
    return judge(windows, reserve)


def judge(windows: dict[str, float], reserve: float) -> Reading:
    """Eligible only when the tightest window has more than `reserve` % left."""
    if not windows:
        return Reading("capacity-unknown", "meter reported no quota window")
    for name, left in windows.items():
        if not 0 <= left <= 100:
            return Reading("capacity-unknown", f"{name} reports {left}% left")
    name, left = min(windows.items(), key=lambda item: item[1])
    status = "eligible" if left > reserve else "reserve-low"
    return Reading(status, f"{name} {left:.0f}% left; reserve {reserve:g}%")


# ------------------------------------------------------------------ codex


def _codex(lane: Lane) -> dict[str, float]:
    return codex_windows(_rpc(["codex", "app-server", "--stdio"], [
        {"method": "initialize", "id": 1,
         "params": {"clientInfo": {"name": "xcheck", "title": "xcheck", "version": "2"}}},
        {"method": "initialized", "params": {}},
        {"method": "account/rateLimits/read", "id": 2},
    ], want=2))


def codex_windows(result: dict) -> dict[str, float]:
    limits = result["rateLimits"]
    return {f"{k} {limits[k]['windowDurationMins']}m": 100.0 - float(limits[k]["usedPercent"])
            for k in ("primary", "secondary") if limits.get(k)}


def _rpc(argv: list[str], messages: list[dict], want: int) -> dict:
    """Send JSON-RPC lines and return the result for id `want`. stdin stays
    open until the reply: the app server exits on EOF without answering."""
    p = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        p.stdin.write(b"".join(json.dumps(m).encode() + b"\n" for m in messages))
        p.stdin.flush()
        deadline, buf = time.time() + TIMEOUT, b""
        while (left := deadline - time.time()) > 0:
            if not select.select([p.stdout], [], [], left)[0]:
                break
            chunk = os.read(p.stdout.fileno(), 65536)
            if not chunk:
                break
            *lines, buf = (buf + chunk).split(b"\n")
            for line in lines:
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if isinstance(msg, dict) and msg.get("id") == want:
                    if "error" in msg:
                        text = str((msg["error"] or {}).get("message", ""))
                        if re.search(r"auth|log ?in|unauthori[sz]ed|sign in", text, re.I):
                            raise LoginRequired("Codex login required: run codex login")
                        raise ValueError(f"app-server error: {text[:120]}")
                    return msg["result"]
        raise TimeoutError("no rate-limit reply from codex app-server")
    finally:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            p.kill()
        p.wait()


# ------------------------------------------------------------------ claude


def _claude(lane: Lane) -> dict[str, float]:
    manual = _claude_manual()
    if manual is not None:
        return manual
    p = subprocess.run(["security", "find-generic-password", "-w", "-s", "Claude Code-credentials"],
                       capture_output=True, text=True, timeout=TIMEOUT)
    if p.returncode == 44:  # errSecItemNotFound
        raise LoginRequired(f"no Claude login in the keychain: run {CLAUDE_LOGIN}")
    if p.returncode != 0:
        raise OSError(f"keychain unreadable (security exit {p.returncode}; 36 = locked: run "
                      f"keychain-unlock-gui, or set {CLAUDE_MANUAL[0]} and {CLAUDE_MANUAL[1]})")
    credentials = json.loads(p.stdout)["claudeAiOauth"]
    expires = credentials.get("expiresAt")
    if "expiresAt" in credentials:
        if isinstance(expires, bool) or not isinstance(expires, (int, float)) or not math.isfinite(expires) or expires <= 0:
            raise ValueError("Claude credential expiry is invalid")
        if expires <= (time.time() + CLAUDE_EXPIRY_MARGIN) * 1000:
            raise LoginRequired(f"Claude OAuth token expired or expires within {CLAUDE_EXPIRY_MARGIN:g}s: "
                                f"open claude to refresh it, or run {CLAUDE_LOGIN}")
    token = credentials["accessToken"]
    # 429 is a throttle shared by every reader of this endpoint (CodexBar polls it too).
    waited, attempt = 0.0, 0
    deadline = time.monotonic() + CLAUDE_THROTTLE_WAIT
    while True:
        remaining = min(CLAUDE_THROTTLE_WAIT - waited, deadline - time.monotonic())
        if remaining <= 0:
            raise TimeoutError("Claude usage endpoint throttle budget exhausted; recheck xcheck capacity")
        try:
            payload = _get("https://api.anthropic.com/api/oauth/usage",
                           {"Authorization": f"Bearer {token}", "anthropic-beta": "oauth-2025-04-20"},
                           f"Claude login expired: run {CLAUDE_LOGIN}", timeout=min(TIMEOUT, remaining))
            break
        except urllib.error.HTTPError as e:
            if e.code != 429:
                raise
            pause = throttle_pause(e.headers.get("Retry-After") if e.headers else None, attempt)
            e.close()
            if attempt >= 4 or pause > min(remaining, deadline - time.monotonic()):
                raise TimeoutError(f"Claude usage endpoint throttled (HTTP 429); retry in {pause:.0f}s; "
                                   "recheck xcheck capacity before a fresh run") from None
            time.sleep(pause)
            waited, attempt = waited + pause, attempt + 1
    bucket = next((b for b in ("opus", "sonnet") if b in lane.model), None)
    return claude_windows(payload, bucket)


def throttle_pause(retry_after: str | None, attempt: int) -> float:
    """A positive finite Retry-After (seconds or HTTP date), else 2, 4, 8, … s.
    Zero/negative/nonfinite values must not create a tight, unbounded loop."""
    try:
        delay = float(retry_after)
    except (TypeError, ValueError):
        try:
            delay = parsedate_to_datetime(retry_after).timestamp() - time.time()
        except (TypeError, ValueError, OverflowError, AttributeError):
            delay = 0
    return delay if math.isfinite(delay) and delay > 0 else 2.0 * 2 ** attempt


def claude_windows(payload: dict, bucket: str | None) -> dict[str, float]:
    """`limits[]` is the current meter (session, weekly_all, and weekly_scoped
    per-model caps, `percent` used); the flat keys are read only without it.
    A scoped cap attributed to the other model family is left out; one that
    cannot be attributed counts (fail closed)."""
    limits = payload.get("limits")
    if isinstance(limits, list) and limits:
        windows = {}
        for i, cap in enumerate(limits):
            scope = json.dumps(cap.get("scope")).lower()
            if any(f in scope for f in ("opus", "sonnet") if f != bucket) and not (bucket and bucket in scope):
                continue
            windows[f"{cap.get('kind')} {i}"] = 100.0 - float(cap["percent"])
        return windows
    names = ["five_hour", "seven_day"] + ([f"seven_day_{bucket}"] if bucket else [])
    return {n: 100.0 - float(payload[n]["utilization"]) for n in names if isinstance(payload.get(n), dict)}


def _claude_manual() -> dict[str, float] | None:
    remaining, at = (os.environ.get(k, "").strip() for k in CLAUDE_MANUAL)
    if not remaining and not at:
        return None
    from datetime import datetime, timezone
    age = (datetime.now(timezone.utc) - datetime.fromisoformat(at.replace("Z", "+00:00"))).total_seconds()
    if not -60 <= age <= CLAUDE_MANUAL_AGE:
        raise ValueError(f"manual Claude reading is {age:.0f}s old (limit {CLAUDE_MANUAL_AGE}s)")
    return {"manual /usage": float(remaining)}


# ------------------------------------------------------------------ grok


def _grok(lane: Lane) -> dict[str, float]:
    try:
        records = json.loads((Path.home() / ".grok" / "auth.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise LoginRequired("no Grok login: run grok login") from None
    logins = [r for r in records.values() if isinstance(r, dict) and r.get("key")]
    if not logins:
        raise LoginRequired("no Grok login: run grok login")
    key = max(logins, key=lambda r: str(r.get("create_time", "")))["key"]
    # A rejected token may only be stale: running `grok` once refreshes it.
    windows = grok_windows(_get("https://cli-chat-proxy.grok.com/v1/billing?format=credits",
                               {"Authorization": f"Bearer {key}", "X-XAI-Token-Auth": "xai-grok-cli"},
                               "Grok token rejected: run grok once to refresh it, or grok login"))
    if not windows:
        # The native unified-billing response can contain only period and
        # balance fields. Zero prepaid/on-demand balances establish no quota.
        raise ValueError("Grok credits view supplied neither creditUsagePercent nor GrokBuild usagePercent; "
                         "quota reserve cannot be verified. Check Grok /usage and recheck "
                         "xcheck capacity --lanes grok:grok-4.7:xhigh before a fresh run")
    return windows


def grok_windows(payload: dict) -> dict[str, float]:
    """The account meter and the Grok Build product meter; either can bind."""
    config = payload["config"]
    period = str((config.get("currentPeriod") or {}).get("type", "")).removeprefix("USAGE_PERIOD_TYPE_").lower()
    windows = {f"{period or 'period'} GrokBuild {i}": 100.0 - float(p["usagePercent"])
               for i, p in enumerate(config.get("productUsage") or [])
               if isinstance(p, dict) and p.get("product") == "GrokBuild"}
    if config.get("creditUsagePercent") is not None:
        windows[f"{period or 'period'} credits"] = 100.0 - float(config["creditUsagePercent"])
    return windows  # keep every window until judge validates it, then takes the minimum


# ------------------------------------------------------------------ agy


def _agy(lane: Lane) -> dict[str, float]:
    version = subprocess.run(["agy", "--version"], capture_output=True, text=True, timeout=TIMEOUT).stdout
    parts = tuple(int(x) for x in re.findall(r"\d+", version)[:3])
    if len(parts) != 3 or parts < AGY_USAGE_SINCE:
        raise ValueError(f"agy {version.strip() or '?'} predates the zero-turn /usage command")
    p = subprocess.run(["agy", "-p", "/usage", "--output-format", "json", "--print-timeout", "90s"],
                       capture_output=True, timeout=AGY_TIMEOUT)
    if re.search(rb"select login method|not logged into antigravity", p.stdout + p.stderr, re.I):
        raise LoginRequired("Agy login required: run agy login")
    report = json.loads(p.stdout)
    if report.get("status") != "SUCCESS" or (report.get("command") or {}).get("name") != "usage":
        raise ValueError(f"agy /usage returned status {report.get('status')!r}")
    return agy_windows(report["command"]["data"], lane.model)


def agy_windows(data: dict, model: str) -> dict[str, float]:
    """The lane's family (Gemini or third-party) has a 5 h and a weekly bucket.
    A disabled 5 h bucket (its fraction is meaningless while the weekly one is
    exhausted) is left out; a missing bucket is an error."""
    family = "gemini" if model.startswith("gemini-") else "3p"
    buckets = {b["id"]: b for g in data["groups"] for b in g.get("buckets") or []}
    windows = {}
    for name in (f"{family}-5h", f"{family}-weekly"):
        b = buckets[name]
        if not b.get("disabled"):
            windows[name] = 100.0 * float(b["remaining_fraction"])
    return windows


# ------------------------------------------------------------------ shared


def _get(url: str, headers: dict, rejected: str, *, timeout: float = TIMEOUT) -> dict:
    req = urllib.request.Request(url, headers={**headers, "User-Agent": "xcheck-capacity/2"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise LoginRequired(rejected) from None
        raise


METERS = {"codex": _codex, "claude": _claude, "grok": _grok, "agy": _agy}
