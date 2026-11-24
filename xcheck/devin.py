"""Check Devin's signed-in model and pricing catalog without a review turn."""

import json
import subprocess
from pathlib import Path

from .environment import lane_env
from .types import Launch


def inventory() -> dict:
    cmd = ["devin", "models", "list", "--format", "json"]
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=30,
                                env=lane_env(Launch([], None, kind="devin"), Path.cwd()))
        if result.returncode:
            return {}
        data = json.loads(result.stdout)
        variants = [row for family in data["families"] for row in family["variants"]]
        models = {row["model_uid"]: row for row in variants}
        if not models or len(models) != len(variants) or not all(
                isinstance(uid, str) and uid for uid in models):
            raise ValueError
        return models
    except (OSError, subprocess.SubprocessError, ValueError, TypeError, KeyError):
        return {}


def model_problem(models: dict, model: str, *, require_free: bool) -> str | None:
    if not models:
        return "inventory-unknown: could not read Devin's model catalog"
    if model not in models:
        return f"model-unavailable: Devin does not list {model}"
    row = models[model]
    if require_free and (row.get("cost_tier") != "Free" or row.get("cost_summary")):
        return f"free-price-unconfirmed: Devin does not list {model} at zero price"
    return None


def free_model_problem(model: str) -> str | None:
    return model_problem(inventory(), model, require_free=True)
