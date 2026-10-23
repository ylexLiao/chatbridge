"""Read the few Codex settings that decide where an imported thread shows up.

Codex lists and resumes threads per model provider (`model_provider`, default
"openai"), so an import has to be tagged with the provider the user's Codex is
configured for. Python 3.10 has no tomllib, so a small reader handles the flat
``key = "value"`` lines these settings use.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

DEFAULT_PROVIDER = "openai"
SETTINGS = ("model_provider", "model", "model_reasoning_effort")

_TABLE_RE = re.compile(r"^\[\s*([^\[\]]+?)\s*\]\s*(?:#.*)?$")
_KEY_RE = re.compile(r"""^([A-Za-z0-9_\-]+|"[^"]*"|'[^']*')\s*=\s*(.+)$""")


def read_codex_settings(codex_home: Path) -> dict[str, str]:
    """Effective model_provider / model / model_reasoning_effort (top level, then active profile)."""
    data = _load_toml(codex_home / "config.toml")
    settings = {key: data[key] for key in SETTINGS if isinstance(data.get(key), str) and data[key]}
    profile = data.get("profile")
    profiles = data.get("profiles")
    if isinstance(profile, str) and isinstance(profiles, dict) and isinstance(profiles.get(profile), dict):
        for key in SETTINGS:
            value = profiles[profile].get(key)
            if isinstance(value, str) and value:
                settings[key] = value
    return settings


def codex_model_provider(codex_home: Path) -> str:
    return read_codex_settings(codex_home).get("model_provider") or DEFAULT_PROVIDER


def _load_toml(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return {}
    try:
        import tomllib  # Python 3.11+
    except ModuleNotFoundError:
        return parse_simple_toml(text)
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return parse_simple_toml(text)


def parse_simple_toml(text: str) -> dict[str, Any]:
    """Parse string/bool/number ``key = value`` pairs in top-level and ``[a.b]`` tables.

    Arrays, inline tables and multi-line strings are skipped: the settings
    ChatBridge needs are plain strings.
    """
    root: dict[str, Any] = {}
    current = root
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        table = _TABLE_RE.match(line)
        if table and not line.startswith("[["):
            current = root
            for part in _split_dotted(table.group(1)):
                child = current.setdefault(part, {})
                if not isinstance(child, dict):
                    child = {}
                    current[part] = child
                current = child
            continue
        pair = _KEY_RE.match(line)
        if not pair:
            continue
        key = _unquote(pair.group(1))
        value = _parse_value(pair.group(2))
        if value is not None:
            current[key] = value
    return root


def _split_dotted(value: str) -> list[str]:
    parts: list[str] = []
    for match in re.finditer(r'"([^"]*)"|\'([^\']*)\'|([^.\s]+)', value):
        parts.append(next(group for group in match.groups() if group is not None))
    return parts


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value


def _parse_value(raw: str) -> Any:
    value = raw.strip()
    if value.startswith('"""') or value.startswith("'''") or value[:1] in {"[", "{"}:
        return None
    if value[:1] == '"':
        out = []
        index = 1
        while index < len(value):
            char = value[index]
            if char == "\\" and index + 1 < len(value):
                nxt = value[index + 1]
                out.append({"n": "\n", "t": "\t", '"': '"', "\\": "\\"}.get(nxt, nxt))
                index += 2
                continue
            if char == '"':
                return "".join(out)
            out.append(char)
            index += 1
        return None
    if value[:1] == "'":
        end = value.find("'", 1)
        return value[1:end] if end != -1 else None
    bare = value.split("#", 1)[0].strip()
    if bare in {"true", "false"}:
        return bare == "true"
    try:
        return int(bare)
    except ValueError:
        pass
    try:
        return float(bare)
    except ValueError:
        return None
