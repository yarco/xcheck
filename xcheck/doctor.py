"""Readiness reporting. Uses admission's native checks; never recovers login."""
import re
import subprocess
import tempfile
from pathlib import Path

from . import adapters, agy, devin, freeroster, grok, install, models, vibe

MODEL_LISTS = {"grok": ["grok", "models"]}


def check() -> int:
    bad = 0
    root, current = install.source_root(), install.installed_digest()
    source = install.digest(root) if root and root.is_dir() else None
    if source is not None and current == source:
        print(f"ok   installed copy matches source {root} ({source})")
    else:
        print(f"FAIL installed copy {current or 'missing'} != source {source or 'missing'}: run bin/xcheck install")
        bad += 1
    for kind, (cmd, flags) in adapters.HELP_COMMANDS.items():
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            text = out.stdout + out.stderr  # agy prints its help on stderr
        except (OSError, subprocess.TimeoutExpired) as e:
            print(f"FAIL {kind}: {' '.join(cmd)}: {e}")
            bad += 1
            continue
        if out.returncode != 0:
            print(f"FAIL {kind}: help command exited {out.returncode}")
            bad += 1
            continue
        missing = [f for f in flags if f not in text]
        print(f"{'FAIL' if missing else 'ok  '} {kind}: " + (f"missing {missing}" if missing else f"{len(flags)} flags present"))
        bad += bool(missing)
    with tempfile.TemporaryDirectory(prefix="xcheck-grok-doctor-") as tmp:
        scratch = Path(tmp)
        work = scratch / "work"
        work.mkdir()
        peer = adapters.Lane("grok", "grok-4.7", "xhigh")
        problem = adapters.unavailable(peer)
        if problem is None:
            spec = adapters.launch(peer, work, scratch / "message.txt", scratch / "harness.log", True)
            problem = grok.admission(work, spec)
    print(f"FAIL grok: {problem}" if problem else
          "ok   grok: --sandbox off startup and resolved-toolset contract (zero inference)")
    bad += bool(problem)
    for kind, ok in (("codex", "Codex CLI present"),
                     ("claude", "native limit flags and a first-party subscription login present")):
        problem = adapters.unavailable(adapters.Lane(kind, ""))
        print(f"FAIL {kind}: {problem}" if problem else f"ok   {kind}: {ok}")
        bad += bool(problem)
    blocked = adapters.unavailable(adapters.Lane("agy", ""))
    stamp = adapters.AGY_ACP_DIR / "VERSION"
    installed = stamp.read_text().strip() if stamp.is_file() else None
    version = installed or "unknown (verify the installed ACP server)"
    print(f"FAIL agy: {blocked}" if blocked else f"ok   agy: acpx, ACP server {version} and Keychain login present")
    bad += bool(blocked)
    listed = agy.registry_version()
    print(f"unknown agy server: ACP registry entry unreadable ({agy.REGISTRY})" if listed is None else
          f"ok   agy server: {listed} matches the ACP registry" if installed == listed else
          f"new     agy server: ACP registry lists {listed}, installed {installed or 'unknown'}; "
          f"replace agy_acp_server.par and localharness_external together from that release")
    # Free lanes come from the roster at launch; a default that names one goes
    # stale the moment the refresh drops it.
    declared, eligible = freeroster.load(adapters.FREE_ROSTER)
    for lane in adapters.DEFAULT_ROSTER:
        if problem := freeroster.default_pin_problem(lane, declared):
            print(f"FAIL {problem}")
            bad += 1
    if freeroster.PER_PROVIDER:
        counts: dict[str, int] = {}
        for lane in eligible:
            counts[freeroster.provider(lane)] = counts.get(freeroster.provider(lane), 0) + 1
        print(f"{'ok  ' if counts else 'FAIL'} free lanes: {freeroster.PER_PROVIDER} per provider from "
              f"{adapters.FREE_ROSTER} (eligible: " +
              (", ".join(f"{p} {n}" for p, n in sorted(counts.items())) or "none") + ")")
        bad += not counts
    pins = tuple(dict.fromkeys((*adapters.DEFAULT_ROSTER, *adapters.OPT_IN)))  # a pin may also be a local default
    providers = {"opencode"} | {p.model.split("/", 1)[0] for p in pins if p.kind == "opencode"}
    if any(p.kind == "cline" for p in pins):
        providers.add("cline")
    try:
        report = models.discover(providers)
    except Exception as error:
        print(f"unknown opencode discovery: {type(error).__name__}")
        report = {}
    roster = models.classify_roster(report, pins, adapters.DEFERRED)
    verdicts = roster["verdicts"]
    # Reuse provider-scoped client observations from discovery; native catalog
    # support remains separate from the provider's live pricing verdict.
    catalogs, listings = {}, {}
    devin_models = devin.inventory() if any(peer.kind == "devin" for peer in pins) else {}
    for peer in pins:
        if peer.kind == "agy":
            # The same zero-turn check lane admission runs, per pinned selector.
            problem = "unchecked: Agy startup is unavailable (above)" if blocked else agy.admission(peer)
            print(f"FAIL agy model {peer.model}: {problem}" if problem else
                  f"ok   agy model {peer.model} advertised by the ACP server" +
                  (f" with thinking level {peer.effort}" if peer.effort else ""))
            bad += bool(problem)
        elif peer.kind == "vibe":
            problem = adapters.unavailable(peer) or vibe.admission(peer)
            print(f"FAIL vibe model {peer.model}: {problem}" if problem else
                  f"ok   vibe model {peer.model} offered to the active Mistral account" +
                  (f" with thinking level {peer.effort}" if peer.effort else ""))
            bad += bool(problem)
        elif peer.kind == "cline":
            problem = adapters.unavailable(peer)
            verdict = verdicts.get(f"cline:{peer.model}", "unknown")
            print(f"{'FAIL' if problem or verdict == 'gone' else 'ok  '} cline pin {peer.model}: {problem or verdict}")
            bad += bool(problem) or verdict == "gone"
        elif peer.kind == "opencode":
            provider = peer.model.split("/", 1)[0]
            if provider not in catalogs:
                catalogs[provider] = client_catalog(provider, report)
            catalog, problem = catalogs[provider]
            ok = problem is None and peer.model in catalog
            verdict = verdicts.get(f"opencode:{peer.model}", "unknown")
            print(f"{verdict:8}opencode pin {peer.model}: " +
                  (f"unchecked: {problem}" if problem else "listed by model API" if ok else
                   "NOT in opencode api model.list --standalone"))
            bad += not ok or verdict == "gone"
        elif peer.kind == "devin":
            require_free = peer == adapters.DEVIN_FREE_DEFAULT
            problem = devin.model_problem(devin_models, peer.model, require_free=require_free)
            status = ("listed as Free" if require_free else "listed") if problem is None else problem
            print(f"{'FAIL' if problem else 'ok  '} devin model {peer.model}: {status}")
            bad += bool(problem)
        elif peer.kind in MODEL_LISTS:
            cmd = MODEL_LISTS[peer.kind]
            if peer.kind not in listings:
                listings[peer.kind] = listing(cmd)
            text, problem = listings[peer.kind]
            if problem:
                print(f"FAIL {peer.kind} model {peer.model} unchecked: {problem}")
                bad += 1
            else:
                ok = re.search(r"(?m)^[ \t]*(?:[-*][ \t]+)?" + re.escape(peer.model) + r"(?=[ \t]|$)", text) is not None
                print(f"{'ok  ' if ok else 'FAIL'} {peer.kind} model {peer.model} {'listed' if ok else 'NOT in ' + ' '.join(cmd)}")
                bad += not ok
    for account, store in adapters.OPENCODE_ACCOUNTS.items():
        problem = adapters.account_problem(store)
        print(f"FAIL opencode account {account}: {problem}" if problem else
              f"ok   opencode account {account}: Zen credential stored in {store}")
        bad += bool(problem)
    for selector in sorted(s for s, mark in roster["marks"].items() if mark == "--add" and s.startswith("opencode:opencode/")):
        print(f"new     {selector}: ready Zen offer, not in the free roster yet; the daily refresh adds it once its lane check ends done")
    return 1 if bad else 0


def listing(cmd):
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        text = out.stdout + out.stderr
        problem = (f"model listing exited {out.returncode}" if out.returncode else
                   "model listing returned no output" if not text.strip() else None)
        return text, problem
    except (OSError, subprocess.TimeoutExpired) as error:
        return "", f"model listing failed: {type(error).__name__}"


def client_catalog(provider, report):
    read = (report.get("providers") or {}).get(provider) or {}
    if "client_ids" in read:
        return set(read["client_ids"]), None
    if "client_error" in read:
        return set(), f"model API failed: {read['client_error']}"
    try:
        return set(models.review_metadata(provider)), None
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        return set(), f"model API failed: {type(error).__name__}"
