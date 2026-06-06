from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class Message:
    role: str
    text: str
    timestamp: str | int | float | None = None


@dataclass
class Artifact:
    kind: str
    text: str
    path: str | None = None


@dataclass
class Session:
    source: str
    session_id: str
    title: str = "Untitled"
    project_path: str | None = None
    created_at: str | int | float | None = None
    updated_at: str | int | float | None = None
    messages: list[Message] = field(default_factory=list)
    artifacts: list[Artifact] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    raw_path: Path | None = None

    @property
    def source_label(self) -> str:
        return {"copilot": "Copilot", "codex": "Codex", "claude": "Claude Code"}.get(self.source, self.source.title())
