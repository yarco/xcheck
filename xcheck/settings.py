"""Optional machine-local settings; never copied into the installed runtime."""

from __future__ import annotations

import json
import os
from pathlib import Path


def load() -> dict:
    explicit = os.environ.get("XCHECK_CONFIG")
    path = Path(explicit).expanduser() if explicit else Path.home() / ".config/xcheck/config.json"
    if not path.is_absolute():
        raise ValueError("XCHECK_CONFIG must be an absolute path or start with ~/")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        if not explicit:
            return {}
        raise ValueError("XCHECK_CONFIG does not exist") from error
    except (OSError, ValueError) as error:
        # Config contents and OS error strings can contain private data.
        raise ValueError("cannot read xcheck local configuration as JSON") from error
    paths = {"caller_context_helper", "free_roster", "agy_rotate_command"}
    keys = paths | {"default_lanes", "opencode_accounts", "opencode_lanes_per_account"}
    if not isinstance(data, dict) or data.keys() - keys:
        raise ValueError("xcheck local configuration has unknown keys or is not an object")
    for key in paths:
        if key in data:
            value = data[key]
            if not isinstance(value, str) or not value.strip() or "\0" in value:
                raise ValueError(f"{key} must be an absolute path or start with ~/")
            path = Path(value).expanduser()
            if not path.is_absolute():
                raise ValueError(f"{key} must be an absolute path or start with ~/")
            data[key] = path
    if "opencode_accounts" in data:
        accounts = data["opencode_accounts"]
        if not isinstance(accounts, dict) or not accounts or not all(
                isinstance(k, str) and k.strip() and isinstance(v, str) and v.strip() and "\0" not in v
                and Path(v).expanduser().is_absolute() for k, v in accounts.items()):
            raise ValueError("opencode_accounts must map account names to absolute store paths or paths starting with ~/")
        stored = {k: Path(v).expanduser() for k, v in accounts.items()}
        # `/tmp/a.db` and `/tmp/a.db/.`, or a symlink to the same file, are one
        # login. Counting them twice would double that account's lane budget.
        identities = []
        for path in stored.values():
            try:
                identity = path.resolve()
            except OSError:
                identity = path
            identities.append(identity)
        if len(set(identities)) != len(identities):
            raise ValueError("opencode_accounts names one store twice")
        data["opencode_accounts"] = stored
    if "opencode_lanes_per_account" in data:
        limit = data["opencode_lanes_per_account"]
        if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
            raise ValueError("opencode_lanes_per_account must be a positive integer")
    if "default_lanes" in data:
        lanes = data["default_lanes"]
        if not isinstance(lanes, list) or not lanes or not all(isinstance(x, str) for x in lanes):
            raise ValueError("default_lanes must be a non-empty list of lane selectors")
    return data


def apply() -> None:
    """Apply only explicit overrides; absent config keeps the existing setup."""
    from . import adapters, capacity, herdr

    data = load()
    if "default_lanes" in data:
        try:
            lanes = tuple(adapters.parse_lane(x) for x in data["default_lanes"])
        except ValueError as error:
            raise ValueError("default_lanes contains an invalid lane selector") from error
        if len({lane.name for lane in lanes}) != len(lanes):
            raise ValueError("default_lanes contains duplicate lane names")
        adapters.DEFAULT_ROSTER = lanes
    if "caller_context_helper" in data:
        herdr.CONTEXT_HELPER = data["caller_context_helper"]
    if "opencode_accounts" in data:
        adapters.OPENCODE_ACCOUNTS = data["opencode_accounts"]
    if "opencode_lanes_per_account" in data:
        adapters.OPENCODE_LANES_PER_ACCOUNT = data["opencode_lanes_per_account"]
    if "agy_rotate_command" in data:
        capacity.AGY_ROTATE = data["agy_rotate_command"]
    if "free_roster" in data:
        adapters.FREE_ROSTER = data["free_roster"]
        adapters.OPT_IN = (adapters.Lane("devin", "swe-2-max"), *adapters.free_lanes(adapters.FREE_ROSTER))
