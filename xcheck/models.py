"""Provider-owned free offers. Metadata only: no inference or configuration edits.

Prices and availability come from providers, never Models.dev. OpenCode's local
metadata supplies client support and Zen capabilities/release dates only.
"""
from __future__ import annotations

import fcntl
import json
import subprocess
import urllib.request
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit

from .adapters import parse_lane

ZEN = "https://opencode.ai/zen/v1/models"
ZEN_DOCS = "https://opencode.ai/docs/zen/"
OPENROUTER = "https://openrouter.ai/api/v1/models"
SOURCES = {
    "opencode": {"format": "zen", "models_url": ZEN, "docs_url": ZEN_DOCS,
                 "auth_env": ["OPENCODE_API_KEY"]},
    "openrouter": {"format": "priced-json", "models_url": OPENROUTER,
                   "docs_url": "https://openrouter.ai/docs/guides/routing/model-variants/free",
                   "auth_env": ["OPENROUTER_API_KEY"]},
    "cerebras": {"format": "priced-json",
                 "models_url": "https://api.cerebras.ai/public/v1/models?format=openrouter",
                 "docs_url": "https://inference-docs.cerebras.ai/support/rate-limits",
                 "auth_env": ["CEREBRAS_API_KEY"]},
}


def zero_cost(cost: dict, required: tuple[str, str]) -> bool:
    """Known input/output and every reported charge must be exactly zero."""
    if not isinstance(cost, dict) or any(k not in cost for k in required):
        return False
    for value in cost.values():
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            return False
        try:
            number = Decimal(str(value))
        except InvalidOperation:
            return False
        if not number.is_finite() or number != 0:
            return False
    return True


class Tables(HTMLParser):
    """Read complete table cells from the production docs, without an LLM."""
    def __init__(self):
        super().__init__()
        self.tables, self.table, self.row, self.cell = [], None, None, None

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self.table = []
        if self.table is not None and tag == "tr":
            self.row = []
        if self.row is not None and tag in ("td", "th"):
            self.cell = ""

    def handle_data(self, data):
        if self.cell is not None:
            self.cell += data

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self.cell is not None:
            self.row.append(self.cell.strip())
            self.cell = None
        if tag == "tr" and self.row is not None:
            self.table.append(self.row)
            self.row = None
        if tag == "table" and self.table is not None:
            self.tables.append(self.table)
            self.table = None


def zen_candidates(payload: dict, html: str) -> list[dict]:
    """Join documented free prices and chat endpoints to live IDs. No ID pins."""
    parser = Tables()
    parser.feed(html)
    endpoints = [t for t in parser.tables if t and t[0] == ["Model", "Model ID", "Endpoint", "AI SDK Package"]]
    pricing = [t for t in parser.tables if t and t[0][:3] == ["Model", "Input", "Output"]]
    if len(endpoints) != 1 or len(pricing) != 1:
        raise ValueError("Zen documentation tables changed; no prices confirmed")
    live = {m["id"] for m in payload["data"]}
    prices = {}
    for row in pricing[0][1:]:
        if len(row) != len(pricing[0][0]):
            raise ValueError("incomplete Zen pricing row")
        if row[0] in prices:
            raise ValueError("ambiguous Zen pricing row")
        # '-' is only a missing optional charge, never a free input/output price.
        charges = {h: ("0" if v == "Free" or (i > 2 and v == "-") else v.removeprefix("$"))
                   for i, (h, v) in enumerate(zip(pricing[0][0], row)) if i}
        prices[row[0]] = charges
    result = []
    names, identifiers = set(), set()
    for row in endpoints[0][1:]:
        if len(row) != 4:
            raise ValueError("incomplete Zen endpoint row")
        name, model, endpoint, _ = row
        if name in names or model in identifiers:
            raise ValueError("ambiguous Zen endpoint row")
        names.add(name)
        identifiers.add(model)
        if model not in live or not endpoint.endswith(("/chat/completions", "/responses", "/messages")):
            continue
        if zero_cost(prices.get(name), ("Input", "Output")):
            result.append({"model": model, "name": name, "release_date": "",
                           "date_source": "", "tool_call": None, "pricing_source": ZEN_DOCS})
    return result


def priced_candidates(payload: dict, source_url: str) -> list[dict]:
    result = []
    for item in payload["data"]:
        architecture = item.get("architecture", {})
        inputs = architecture.get("input_modalities", item.get("input_modalities", []))
        outputs = architecture.get("output_modalities", item.get("output_modalities", []))
        features = item.get("supported_parameters", item.get("supported_features", []))
        # A review lane is a text coding model: multimodal (image/audio/video)
        # models are excluded, and so is any offer with an announced end date,
        # which is a promotion rather than a standing free tier.
        if ("tools" not in features or set(inputs) != {"text"} or set(outputs) != {"text"}
                or architecture.get("tokenizer") == "Router" or item.get("router") is True
                or item.get("deprecated") or item.get("expiration_date")
                or not zero_cost(item.get("pricing"), ("prompt", "completion"))):
            continue
        created = item.get("created")
        released = ""
        if isinstance(created, (int, float)) and not isinstance(created, bool):
            try:
                released = datetime.fromtimestamp(created, timezone.utc).date().isoformat()
            except (ValueError, OverflowError, OSError):
                pass
        result.append({"model": item["id"], "name": item.get("name", item["id"]),
                       "release_date": released, "date_source": source_url if released else "",
                       "tool_call": True, "pricing_source": source_url})
    return result


def fetch(url: str, *, as_json: bool = True):
    req = urllib.request.Request(url, headers={"User-Agent": "xcheck-models/2", "Cache-Control": "no-cache"})
    with urllib.request.urlopen(req, timeout=20) as response:
        body = response.read().decode("utf-8")
    return json.loads(body) if as_json else body


def client_metadata(*, refresh: bool = True, env: dict | None = None, cwd: Path | None = None) -> dict:
    # Lanes of one run list at the same moment; a listing that overlaps
    # OpenCode refreshing its models cache once came back truncated. One
    # listing at a time per xcheck root.
    from .files import ROOT
    ROOT.mkdir(parents=True, exist_ok=True)
    with open(ROOT / ".opencode-models.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        listing = subprocess.run(["opencode", "models", "--verbose", *(["--refresh"] if refresh else [])],
                                 env=env, cwd=cwd, capture_output=True, text=True, timeout=60, check=True)
    decoder = json.JSONDecoder()
    remaining = listing.stdout.strip()
    result = {}
    while remaining:
        selector, separator, remaining = remaining.partition("\n")
        if not separator:
            raise ValueError("OpenCode model listing format changed")
        item, end = decoder.raw_decode(remaining.lstrip())
        remaining = remaining.lstrip()[end:].lstrip()
        result[selector.strip()] = item
    return result


def sources(path: Path | None = None) -> dict:
    result = dict(SOURCES)
    if path is not None:
        extra = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(extra, dict):
            raise ValueError("sources file must map provider IDs to source definitions")
        result.update(extra)
    for provider, source in result.items():
        if parse_lane(f"opencode:{provider}/model").model != f"{provider}/model" or "/" in provider:
            raise ValueError("invalid source provider ID")
        if source["format"] not in ("zen", "priced-json"):
            raise ValueError("source format must be zen or priced-json")
        for field in ("models_url", "docs_url"):
            url = urlsplit(source[field])
            if url.scheme != "https" or not url.hostname or url.username or url.password:
                raise ValueError(f"{field} must be a public HTTPS URL without credentials")
        env = source.get("auth_env", [])
        if not isinstance(env, list) or any(not isinstance(v, str) or not v.isidentifier() for v in env):
            raise ValueError("auth_env must contain variable names only")
    return result


def discover(providers: set[str] | None = None, source_file: Path | None = None) -> dict:
    definitions = sources(source_file)
    if providers and (unknown := providers - definitions.keys()):
        raise ValueError(f"no primary discovery source for: {', '.join(sorted(unknown))}; add a --sources file")
    warnings, result = [], []
    try:
        metadata = client_metadata()
    except Exception as error:
        metadata = {}
        warnings.append(f"OpenCode metadata unavailable; offers need client setup: {type(error).__name__}")
    for provider, source in definitions.items():
        if providers and provider not in providers:
            continue
        try:
            payload = fetch(source["models_url"])
            rows = (zen_candidates(payload, fetch(source["docs_url"], as_json=False))
                    if source["format"] == "zen" else priced_candidates(payload, source["models_url"]))
            confirmed = []
            for row in rows:
                selector = f"opencode:{provider}/{row['model']}"
                try:
                    lane = parse_lane(selector)
                except ValueError:
                    warnings.append(f"{provider} has a model ID unsupported by the lane selector; omitted")
                    continue
                if lane.model != f"{provider}/{row['model']}" or lane.effort:
                    warnings.append(f"{provider} has a model ID unsupported by the lane selector; omitted")
                    continue
                local = metadata.get(lane.model, {})
                caps = local.get("capabilities", {})
                if source["format"] == "zen":
                    row["pricing_source"] = source["docs_url"]
                    row["tool_call"] = caps.get("toolcall")
                    if row["tool_call"] is False:
                        continue
                    if row["tool_call"] is True and not (caps.get("input", {}).get("text") and caps.get("output", {}).get("text")):
                        continue
                    try:
                        row["release_date"] = date.fromisoformat(local.get("release_date", "")).isoformat()
                        row["date_source"] = "OpenCode client metadata (Models.dev); advisory"
                    except (ValueError, TypeError):
                        pass
                client_tools = (caps.get("toolcall") is True and caps.get("input", {}).get("text") is True
                                and caps.get("output", {}).get("text") is True)
                ready = bool(local) and local.get("status") != "deprecated" and row["tool_call"] is True and client_tools
                confirmed.append({**row, "provider": provider, "selector": selector,
                               "source": source["models_url"], "provider_docs": source["docs_url"],
                               "auth_env": source.get("auth_env", []), "client_available": bool(local),
                               "state": "ready" if ready else "needs-client-setup",
                               "access": "verify account quota and provider data policy"})
            result.extend(confirmed)
        except Exception as error:
            warnings.append(f"{provider} primary source failed; offers omitted: {type(error).__name__}")
    # Undated new offers must survive the default display limit despite lagging
    # client metadata. Dated offers retain newest-first order after them.
    result.sort(key=lambda r: (not r["release_date"], r["release_date"], r["provider"], r["model"]), reverse=True)
    return {"checked_at": datetime.now(timezone.utc).isoformat(), "models": result, "warnings": warnings,
            "sources": {k: v for k, v in definitions.items() if not providers or k in providers},
            "notice": "Provider-confirmed zero-price offers. Verify account quota and data policy; connect before opting in. Dates are advisory. No inference or config changes."}
