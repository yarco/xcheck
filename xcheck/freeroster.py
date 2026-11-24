"""Free lanes for a default run, chosen at launch from the configured free roster.

The roster file supplies rotating free OpenCode and Cline lanes. With `free_lanes_per_provider` set, `xcheck run` without `--lanes` adds up
to that many lanes per provider. A lane is eligible only when the roster's
latest refresh observed it working and this machine's client is ready. No two
chosen lanes share a model family (one model served by several providers counts
once). Providers take turns, so every provider gets a first lane before any gets
a second, and the order within a provider rotates per run so successive reviews
spread across the roster.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from pathlib import Path

from .types import Lane

PER_PROVIDER = 0  # settings: free_lanes_per_provider; 0 keeps free lanes opt-in


def provider(lane: Lane) -> str:
    """The free provider a lane spends: cline, opencode (Zen), kilo, openrouter, …"""
    if lane.kind == "cline":
        return "cline"
    return lane.model.split("/", 1)[0] if "/" in lane.model else "opencode"


def family(lane: Lane) -> str:
    """The served model, independent of provider prefix and free-offer suffix."""
    name = lane.model.rsplit("/", 1)[-1].lower()
    return re.sub(r"(?::free|-free)$", "", name)


def free_shaped(lane: Lane) -> bool:
    """A selector that names a free offer by its own spelling."""
    return lane.kind == "cline" or (lane.kind == "opencode" and (
        provider(lane) == "kilo" or lane.model.endswith((":free", "-free"))))


def load(path: Path) -> tuple[tuple[Lane, ...], list[Lane]]:
    """Read one roster snapshot and return its declared lanes and eligible subset."""
    from .adapters import parse_free_lanes

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return (), []
    if not isinstance(data, dict):
        return (), []
    declared = parse_free_lanes(data.get("lanes"))
    records, updated, readiness = (data.get(key) for key in ("models", "updated_at", "client_readiness"))
    states = readiness.get("states") if isinstance(readiness, dict) else None
    if not isinstance(records, dict) or not isinstance(states, dict) or not isinstance(updated, str) or not updated:
        return declared, []
    eligible = []
    for lane in declared:
        selector = f"{lane.kind}:{lane.model}" + (f":{lane.effort}" if lane.effort else "")
        record = records.get(selector)
        if not isinstance(record, dict) or states.get(selector) != "ready":
            continue
        strikes = record.get("ping_strikes")
        if type(strikes) is not int or strikes != 0 or record.get("last_probe_at") != updated:
            continue
        ping = record.get("last_ping")
        if ping == "listed; ping answered" or (ping is None and record.get("onboarded") == updated):
            eligible.append(lane)
    return declared, eligible


def default_pin_problem(lane: Lane, declared: Sequence[Lane]) -> str | None:
    """Identify recognized free defaults even if the pinned effort differs."""
    if free_shaped(lane) or any(
        lane.kind == entry.kind and provider(lane) == provider(entry) and family(lane) == family(entry)
        for entry in declared
    ):
        return (f"default lane {lane.name} is a free offer: remove it from default_lanes; "
                "free lanes come from the free roster (free_lanes_per_provider)")
    return None


def select(candidates: list[Lane], per_provider: int, seed: str, taken: Sequence[Lane] = ()) -> list[Lane]:
    """Up to `per_provider` lanes per provider, no two (nor any in `taken`) sharing a model family.

    Choosing greedily can starve a provider whose only other model a provider
    earlier in turn took, so this is a maximum bipartite matching between
    provider slots and families (augmenting paths). Slots are filled round by
    round across providers, and each provider tries its lanes in this run's
    rotated order, so the rotation decides among equally large choices."""
    if per_provider <= 0:
        return []
    blocked = {family(l) for l in taken if l.kind in ("opencode", "cline")}
    names = {l.name for l in taken}
    offers: dict[str, list[Lane]] = {}
    for lane in sorted(candidates, key=lambda l: l.name):
        if lane.name not in names and family(lane) not in blocked:
            group = offers.setdefault(provider(lane), [])
            if family(lane) not in {family(l) for l in group}:
                group.append(lane)
    for name, lanes in offers.items():
        turn = int(hashlib.sha256(f"{seed}\0{name}".encode()).hexdigest(), 16) % len(lanes)
        offers[name] = lanes[turn:] + lanes[:turn]
    slots = sorted((index, name) for name, lanes in offers.items()
                   for index in range(min(per_provider, len(lanes))))
    holder: dict[str, tuple[int, str]] = {}  # family -> slot

    def assign(slot: tuple[int, str], seen: set[str]) -> bool:
        for lane in offers[slot[1]]:
            fam = family(lane)
            if fam in seen:
                continue
            seen.add(fam)
            if fam not in holder or assign(holder[fam], seen):
                holder[fam] = slot
                return True
        return False

    for slot in slots:
        assign(slot, set())
    by_slot = {slot: fam for fam, slot in holder.items()}
    return [next(l for l in offers[slot[1]] if family(l) == by_slot[slot]) for slot in slots if slot in by_slot]


def default_lanes(roster: Sequence[Lane], orch: str, base: Path, path: Path,
                  added: Sequence[Lane] = ()) -> tuple[list[Lane], list[Lane]]:
    """Return the configured and automatic selections, respecting explicit add-ons."""
    declared, eligible = load(path)
    for lane in roster:
        if problem := default_pin_problem(lane, declared):
            raise ValueError(problem)
    lanes = [lane for lane in roster if lane.kind != orch] + list(added)
    free = [lane for lane in eligible if lane.kind != orch]
    return lanes, select(free, PER_PROVIDER, str(base.resolve()), lanes)
