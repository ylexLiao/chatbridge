from __future__ import annotations

import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import unquote, urlsplit

SECRET_PATTERNS = [
    re.compile(r"(gh[pousr]_[A-Za-z0-9_]{8,})"),
    re.compile(r"(sk-[A-Za-z0-9_-]{12,})"),
    re.compile(r'(?i)((?:token|api[_-]?key|secret|cookie|pwd|[a-z0-9_]*pass(?:word)?)\s*[=:]\s*[\'"]?)([^\s\'"]+)'),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
]
IMAGE_DATA_URL_RE = re.compile(r"data:(image/[A-Za-z0-9.+-]+);base64,([A-Za-z0-9+/=\s]+)")


def now_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def read_json(path: Path) -> Any | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def iter_jsonl(path: Path) -> Iterable[Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue
    except FileNotFoundError:
        return


def append_jsonl(path: Path, row: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def write_jsonl(path: Path, rows: Iterable[Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def redact(text: str) -> str:
    safe = text
    for pattern in SECRET_PATTERNS:
        if pattern.flags & re.S:
            safe = pattern.sub("[REDACTED_PRIVATE_KEY]", safe)
        elif pattern.pattern.startswith("(?i)("):
            safe = pattern.sub(lambda m: m.group(1) + "[REDACTED]", safe)
        else:
            safe = pattern.sub("[REDACTED]", safe)
    return safe


def text_from_any(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return sanitize_embedded_images(value)
    if isinstance(value, list):
        return "\n".join(filter(None, (text_from_any(item) for item in value)))
    if isinstance(value, dict):
        image_text = _image_placeholder_from_mapping(value)
        if image_text:
            return image_text
        for key in ("text", "value", "content", "message"):
            if key in value:
                result = text_from_any(value[key])
                if result:
                    return result
        if value.get("type") == "text" and "text" in value:
            return str(value["text"])
        return " ".join(filter(None, (text_from_any(v) for v in value.values())))
    return str(value)


def sanitize_embedded_images(text: str) -> str:
    clean = re.sub(r"(?i)\binput_image\s+(?=data:image/)", "", str(text))
    return IMAGE_DATA_URL_RE.sub(lambda match: _embedded_image_placeholder(match.group(1), match.group(2)), clean)


def _image_placeholder_from_mapping(value: dict[str, Any]) -> str:
    image_type = str(value.get("type") or value.get("kind") or "").lower()
    if image_type not in {"input_image", "image", "image_url"}:
        return ""
    for key in ("image_url", "url", "data", "source", "path"):
        candidate = value.get(key)
        if isinstance(candidate, str):
            if candidate.startswith("data:image/"):
                return sanitize_embedded_images(candidate)
            if candidate.strip():
                return f"[Image attachment not imported: {candidate.strip()}]"
    return "[Image attachment not imported: source image unavailable]"


def _embedded_image_placeholder(mime_type: str, payload: str) -> str:
    clean_payload = re.sub(r"\s+", "", payload)
    byte_count = _base64_decoded_size(clean_payload)
    suffix = f", approx {format_bytes(byte_count)}" if byte_count else ""
    image_type = mime_type.split("/", 1)[1].upper()
    return f"[Image attachment not imported: embedded {image_type} data URL{suffix}]"


def _base64_decoded_size(payload: str) -> int:
    if not payload:
        return 0
    padding = len(payload) - len(payload.rstrip("="))
    return max(0, (len(payload) * 3) // 4 - padding)


def format_bytes(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    value = float(size)
    for unit in ("KB", "MB", "GB"):
        value /= 1024
        if value < 1024 or unit == "GB":
            return f"{value:.1f} {unit}"
    return f"{value:.1f} GB"


def project_to_claude_slug(project_path: str) -> str:
    normalized = project_path.strip() or "workspace"
    normalized = file_uri_to_path(normalized) or normalized
    return re.sub(r"[^A-Za-z0-9]+", "-", normalized) or "-"


def file_uri_to_path(value: str | None) -> str | None:
    if not value:
        return None
    if value.lower().startswith("file://"):
        parsed = urlsplit(value)
        path = unquote(parsed.path)
        authority = unquote(parsed.netloc)
        if authority and authority.lower() != "localhost":
            if _looks_like_windows_drive(authority):
                return f"{authority}{path}"
            return f"//{authority}{path}"
        if _looks_like_windows_drive_path(path):
            return _normalize_windows_drive_path(path)
        return path
    return value


def _looks_like_windows_drive(value: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z]:", value))


def _looks_like_windows_drive_path(value: str) -> bool:
    return bool(re.match(r"^/[A-Za-z]:/", value) or re.match(r"^/[A-Za-z]\|/", value))


def _normalize_windows_drive_path(value: str) -> str:
    path = value.lstrip("/")
    if re.match(r"^[A-Za-z]\|/", path):
        return f"{path[0]}:{path[2:]}"
    return path


def backup_paths(home: Path, paths: Iterable[Path]) -> Path:
    backup_root = home / ".chatbridge" / "backups" / now_stamp()
    for path in paths:
        if not path.exists():
            continue
        rel = path.relative_to(home) if path.is_relative_to(home) else Path(path.name)
        dest = backup_root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        if path.is_dir():
            shutil.copytree(path, dest, dirs_exist_ok=True)
        else:
            shutil.copy2(path, dest)
    return backup_root
