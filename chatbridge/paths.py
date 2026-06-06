from __future__ import annotations

import json
import os
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any


CONFIG_RELATIVE_PATH = ".chatbridge/config.json"
PATH_SET_ARGS = {
    "copilot_workspace_storage": "--copilot-workspace-storage",
    "codex_home": "--codex-home",
    "claude_home": "--claude-home",
}


@dataclass(frozen=True)
class PathConfig:
    copilot_workspace_storage: Path
    codex_home: Path
    claude_home: Path


@dataclass(frozen=True)
class PathProbe:
    key: str
    label: str
    active: Path
    candidates: list[Path]
    exists: bool
    source: str


def resolve_paths(home: Path) -> PathConfig:
    configured = _configured_paths(home)
    return PathConfig(
        copilot_workspace_storage=_path_override(
            home,
            configured,
            "copilot_workspace_storage",
            "CHATBRIDGE_COPILOT_WORKSPACE_STORAGE",
            _copilot_workspace_candidates(home),
        ),
        codex_home=_path_override(home, configured, "codex_home", "CHATBRIDGE_CODEX_HOME", _codex_home_candidates(home)),
        claude_home=_path_override(home, configured, "claude_home", "CHATBRIDGE_CLAUDE_HOME", _claude_home_candidates(home)),
    )


def path_doctor(home: Path) -> str:
    lines = ["ChatBridge Path Doctor", ""]
    config_path = path_config_path(home)
    lines.append(f"Config file: {config_path}")
    lines.append(f"Edit manually: chatbridge paths edit")
    lines.append("")
    for probe in path_probes(home):
        status = "OK" if probe.exists else "MISSING"
        lines.append(f"{probe.key} [{status}] ({probe.source})")
        lines.append(f"  active: {probe.active}")
        if not probe.exists:
            arg = PATH_SET_ARGS.get(probe.key, f"--{probe.key.replace('_', '-')}")
            lines.append(f"  fix: chatbridge paths set {arg} /absolute/path")
        lines.append("  candidates:")
        for candidate in probe.candidates:
            marker = "*" if candidate == probe.active else "-"
            exists = " exists" if candidate.exists() else ""
            lines.append(f"    {marker} {candidate}{exists}")
        lines.append("")
    lines.append("Set overrides with: chatbridge paths set --copilot-workspace-storage PATH --codex-home PATH --claude-home PATH")
    lines.append("Open config with: chatbridge paths edit")
    return "\n".join(lines).rstrip() + "\n"


def path_probes(home: Path) -> list[PathProbe]:
    configured = _configured_paths(home)
    specs = [
        ("copilot_workspace_storage", "GitHub Copilot / VS Code", "CHATBRIDGE_COPILOT_WORKSPACE_STORAGE", _copilot_workspace_candidates(home)),
        ("codex_home", "Codex CLI", "CHATBRIDGE_CODEX_HOME", _codex_home_candidates(home)),
        ("claude_home", "Claude Code", "CHATBRIDGE_CLAUDE_HOME", _claude_home_candidates(home)),
    ]
    probes: list[PathProbe] = []
    for key, label, env_key, candidates in specs:
        active, source = _path_override_with_source(home, configured, key, env_key, candidates)
        probes.append(PathProbe(key=key, label=label, active=active, candidates=_unique_paths([active, *candidates]), exists=active.exists(), source=source))
    return probes


def set_path_overrides(
    home: Path,
    copilot_workspace_storage: str | None = None,
    codex_home: str | None = None,
    claude_home: str | None = None,
) -> Path:
    path = home / CONFIG_RELATIVE_PATH
    data = _read_config(path)
    paths = data.setdefault("paths", {})
    if not isinstance(paths, dict):
        paths = {}
        data["paths"] = paths
    updates = {
        "copilot_workspace_storage": copilot_workspace_storage,
        "codex_home": codex_home,
        "claude_home": claude_home,
    }
    for key, value in updates.items():
        if value is not None:
            paths[key] = str(Path(value).expanduser())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def edit_path_overrides(home: Path, editor: str | None = None) -> Path:
    path = ensure_path_config(home)
    command = editor or os.environ.get("VISUAL") or os.environ.get("EDITOR")
    if not command:
        raise SystemExit(
            f"No editor configured. Open this file manually or set VISUAL/EDITOR:\n{path}\n"
            "You can also run: chatbridge paths set --copilot-workspace-storage PATH --codex-home PATH --claude-home PATH"
        )
    args = shlex.split(command)
    if not args:
        raise SystemExit("Editor command is empty.")
    subprocess.run([*args, str(path)], check=True)
    return path


def ensure_path_config(home: Path) -> Path:
    path = path_config_path(home)
    if path.exists():
        return path
    data = {
        "paths": {
            "copilot_workspace_storage": "",
            "codex_home": "",
            "claude_home": "",
        }
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def path_config_path(home: Path) -> Path:
    return home / CONFIG_RELATIVE_PATH


def _configured_paths(home: Path) -> dict[str, str]:
    data = _read_config(path_config_path(home))
    paths = data.get("paths")
    return paths if isinstance(paths, dict) else {}


def _read_config(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _path_override(home: Path, configured: dict[str, str], key: str, env_key: str, candidates: list[Path]) -> Path:
    value, _ = _path_override_with_source(home, configured, key, env_key, candidates)
    return value


def _path_override_with_source(home: Path, configured: dict[str, str], key: str, env_key: str, candidates: list[Path]) -> tuple[Path, str]:
    env_value = os.environ.get(env_key)
    if env_value:
        return _expand_path(home, env_value), "env"
    configured_value = configured.get(key)
    if isinstance(configured_value, str) and configured_value.strip():
        return _expand_path(home, configured_value), "config"
    for candidate in candidates:
        if candidate.exists():
            return candidate, "auto"
    return candidates[0], "default"


def _expand_path(home: Path, value: str) -> Path:
    text = value.strip()
    if text.startswith("~/"):
        return home / text[2:]
    return Path(text).expanduser()


def _copilot_workspace_candidates(home: Path) -> list[Path]:
    appdata = os.environ.get("APPDATA")
    candidates = [
        home / ".config" / "Code" / "User" / "workspaceStorage",
        home / "Library" / "Application Support" / "Code" / "User" / "workspaceStorage",
        home / "AppData" / "Roaming" / "Code" / "User" / "workspaceStorage",
        home / ".config" / "Code - Insiders" / "User" / "workspaceStorage",
        home / "Library" / "Application Support" / "Code - Insiders" / "User" / "workspaceStorage",
        home / ".config" / "VSCodium" / "User" / "workspaceStorage",
        home / "Library" / "Application Support" / "VSCodium" / "User" / "workspaceStorage",
        home / ".config" / "Cursor" / "User" / "workspaceStorage",
        home / "Library" / "Application Support" / "Cursor" / "User" / "workspaceStorage",
    ]
    if appdata:
        candidates.insert(2, Path(appdata) / "Code" / "User" / "workspaceStorage")
    return _unique_paths(candidates)


def _codex_home_candidates(home: Path) -> list[Path]:
    env_home = os.environ.get("CODEX_HOME")
    candidates = [home / ".codex"]
    if env_home:
        candidates.insert(0, Path(env_home).expanduser())
    return _unique_paths(candidates)


def _claude_home_candidates(home: Path) -> list[Path]:
    env_home = os.environ.get("CLAUDE_HOME")
    candidates = [home / ".claude"]
    if env_home:
        candidates.insert(0, Path(env_home).expanduser())
    return _unique_paths(candidates)


def _unique_paths(paths: list[Path]) -> list[Path]:
    seen: set[str] = set()
    result: list[Path] = []
    for path in paths:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        result.append(path)
    return result
