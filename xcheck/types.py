"""Dependency-free values shared by launch, persistence and execution."""
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Lane:
    kind: str
    model: str
    effort: str = ""

    @property
    def name(self) -> str:
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
            provider, _, model = self.model.partition("/") if "/" in self.model else ("opencode", "", self.model)
            return f"opencode-zen-{model}" if provider == "opencode" else f"opencode-{provider}"
        return self.kind

    @property
    def zen(self) -> bool:
        return self.kind == "opencode" and self.pool.startswith("opencode-zen-")

    def to_json(self) -> dict:
        return {"kind": self.kind, "model": self.model, "effort": self.effort}

    @classmethod
    def from_json(cls, data: dict) -> "Lane":
        return cls(data["kind"], data["model"], data.get("effort", ""))


@dataclass
class Launch:
    argv: list[str]
    stdin: Path | None
    env_set: dict[str, str] = field(default_factory=dict)
    kind: str = ""
