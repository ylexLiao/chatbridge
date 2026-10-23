"""Readable rendering of tool activity (shell commands, file edits, web/MCP calls).

Parsers turn each source tool call into a ToolCall, attach its output when the
result row shows up, and render it into the text of a ``role="tool"`` message.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import PurePath
from typing import Any

from .util import file_uri_to_path, text_from_any

OUTPUT_LIMIT = 4000
INPUT_LIMIT = 6000
DIFF_LIMIT = 20000
READ_OUTPUT_LIMIT = 1500

SHELL_NAMES = {"bash", "sh", "zsh", "dash", "fish"}


@dataclass
class ToolCall:
    name: str
    title: str
    body: str = ""
    output: str = ""
    exit_code: int | None = None
    is_error: bool = False
    output_limit: int = OUTPUT_LIMIT
    body_limit: int = INPUT_LIMIT
    timestamp: Any = None

    def add_output(self, text: str) -> None:
        text = str(text or "").rstrip()
        if not text:
            return
        self.output = f"{self.output}\n{text}" if self.output else text

    def render(self) -> str:
        lines = [f"[Tool: {self.name}] {self.title}".rstrip()]
        if self.body:
            lines.append(clip(self.body.rstrip(), self.body_limit))
        if self.output:
            label = "error" if self.is_error else "output"
            lines.append(f"--- {label} ---")
            lines.append(clip(self.output, self.output_limit))
        if self.exit_code not in (None, 0):
            lines.append(f"(exit code {self.exit_code})")
        return "\n".join(lines)


def clip(text: str, limit: int) -> str:
    """Keep the head and tail of long text, marking how much was dropped."""
    text = str(text or "")
    if limit <= 0 or len(text) <= limit:
        return text
    head = (limit * 2) // 3
    tail = max(0, limit - head)
    omitted = len(text) - head - tail
    tail_text = text[-tail:].lstrip("\n") if tail else ""
    return f"{text[:head].rstrip()}\n… [{omitted} chars omitted] …\n{tail_text}"


def shell_command_text(command: Any) -> str:
    if isinstance(command, (list, tuple)):
        parts = [str(part) for part in command]
        if len(parts) >= 3 and PurePath(parts[0]).name.lower() in SHELL_NAMES and parts[1] in {"-c", "-lc", "-l", "-ic"}:
            return parts[-1]
        if len(parts) >= 3 and PurePath(parts[0]).name.lower().startswith(("powershell", "pwsh")):
            for flag in ("-Command", "-command", "-c"):
                if flag in parts:
                    index = parts.index(flag)
                    return " ".join(parts[index + 1 :])
        return " ".join(_quote_arg(part) for part in parts)
    return str(command or "")


def _quote_arg(value: str) -> str:
    if value and all(ch.isalnum() or ch in "-_./=:@%+," for ch in value):
        return value
    return "'" + value.replace("'", "'\\''") + "'"


def shell_call(command: Any, *, name: str = "shell", cwd: Any = None) -> ToolCall:
    script = shell_command_text(command).strip()
    first, _, rest = script.partition("\n")
    title = f"$ {first}"
    workdir = file_uri_to_path(str(cwd)) if cwd else None
    if workdir:
        title += f"   (cwd: {workdir})"
    return ToolCall(name=name, title=title, body=rest)


def file_change_call(changes: Any, *, name: str = "apply_patch") -> ToolCall:
    """Render a Codex-style ``{path: {type, content|unified_diff|diff, move_path}}`` map."""
    blocks: list[str] = []
    paths: list[str] = []
    if isinstance(changes, dict):
        items = list(changes.items())
    elif isinstance(changes, list):
        items = [(str(item.get("path") or ""), item) for item in changes if isinstance(item, dict)]
    else:
        items = []
    for path, change in items:
        if not isinstance(change, dict):
            continue
        kind_value = change.get("type") or change.get("kind")
        kind = str(kind_value.get("type") if isinstance(kind_value, dict) else kind_value or "update")
        move_path = change.get("move_path") or (kind_value.get("move_path") if isinstance(kind_value, dict) else None)
        label = {"add": "Added", "delete": "Deleted", "update": "Updated"}.get(kind, "Changed")
        header = f"{label} {path}" + (f" -> {move_path}" if move_path else "")
        paths.append(str(path))
        diff = change.get("unified_diff") or change.get("diff")
        content = change.get("content")
        if diff:
            body = str(diff).rstrip()
        elif isinstance(content, str) and content:
            prefix = "-" if kind == "delete" else "+"
            body = "\n".join(prefix + line for line in content.rstrip("\n").split("\n"))
        else:
            body = ""
        blocks.append(f"{header}\n{body}".rstrip())
    if not paths:
        return ToolCall(name=name, title="(no file changes recorded)")
    title = paths[0] if len(paths) == 1 else f"{len(paths)} files: {', '.join(paths[:4])}{' …' if len(paths) > 4 else ''}"
    return ToolCall(name=name, title=f"edit {title}", body="\n\n".join(blocks), body_limit=DIFF_LIMIT)


def patch_text_call(patch: str, *, name: str = "apply_patch") -> ToolCall:
    """Codex ``*** Begin Patch`` text (legacy apply_patch input)."""
    paths = []
    for line in str(patch or "").splitlines():
        for marker in ("*** Add File: ", "*** Update File: ", "*** Delete File: "):
            if line.startswith(marker):
                paths.append(line[len(marker) :].strip())
    if not paths:
        title = "patch"
    elif len(paths) == 1:
        title = f"edit {paths[0]}"
    else:
        title = f"edit {len(paths)} files: {', '.join(paths[:4])}{' …' if len(paths) > 4 else ''}"
    return ToolCall(name=name, title=title, body=str(patch or ""), body_limit=DIFF_LIMIT)


def generic_call(name: str, arguments: Any) -> ToolCall:
    args = _parse_json_maybe(arguments)
    summary = _compact_args(args)
    first, _, rest = summary.partition("\n")
    return ToolCall(name=name or "tool", title=first, body=rest)


def plan_call(plan: Any, explanation: Any = None, *, name: str = "update_plan") -> ToolCall:
    lines: list[str] = []
    if explanation:
        lines.append(str(explanation).strip())
    items = plan if isinstance(plan, list) else []
    for item in items:
        if isinstance(item, dict):
            status = str(item.get("status") or "").lower()
            mark = "x" if status in {"completed", "done"} else ("~" if status in {"in_progress", "in-progress", "active"} else " ")
            lines.append(f"- [{mark}] {item.get('step') or item.get('content') or item.get('text') or ''}".rstrip())
        elif item:
            lines.append(f"- {item}")
    if not items and isinstance(plan, str):
        lines.append(plan.strip())
    return ToolCall(name=name, title="plan", body="\n".join(lines))


def claude_tool_call(name: str, tool_input: Any) -> ToolCall:
    """Render a Claude Code tool_use block by tool name."""
    data = tool_input if isinstance(tool_input, dict) else _parse_json_maybe(tool_input)
    if not isinstance(data, dict):
        return generic_call(name, tool_input)
    lowered = name.lower()
    if lowered in {"bash", "powershell"}:
        call = shell_call(data.get("command") or "", name=name)
        description = str(data.get("description") or "").strip()
        if description:
            call.title += f"   # {description}"
        return call
    if lowered == "edit":
        return ToolCall(
            name=name,
            title=f"edit {data.get('file_path') or ''}".rstrip(),
            body=_replacement_diff(data.get("old_string"), data.get("new_string"), bool(data.get("replace_all"))),
            body_limit=DIFF_LIMIT,
        )
    if lowered == "multiedit":
        parts = [
            _replacement_diff(edit.get("old_string"), edit.get("new_string"), bool(edit.get("replace_all")))
            for edit in data.get("edits") or []
            if isinstance(edit, dict)
        ]
        return ToolCall(name=name, title=f"edit {data.get('file_path') or ''}".rstrip(), body="\n@@\n".join(parts), body_limit=DIFF_LIMIT)
    if lowered == "write":
        content = str(data.get("content") or "")
        body = "\n".join("+" + line for line in content.rstrip("\n").split("\n")) if content else ""
        return ToolCall(name=name, title=f"write {data.get('file_path') or ''}".rstrip(), body=body, body_limit=DIFF_LIMIT)
    if lowered == "notebookedit":
        return ToolCall(name=name, title=f"edit notebook {data.get('notebook_path') or ''}".rstrip(), body=str(data.get("new_source") or ""), body_limit=DIFF_LIMIT)
    if lowered == "read":
        extra = []
        if data.get("offset"):
            extra.append(f"offset {data['offset']}")
        if data.get("limit"):
            extra.append(f"limit {data['limit']}")
        suffix = f" ({', '.join(extra)})" if extra else ""
        return ToolCall(name=name, title=f"read {data.get('file_path') or ''}{suffix}".rstrip(), output_limit=READ_OUTPUT_LIMIT)
    if lowered in {"grep", "glob"}:
        where = f" in {data['path']}" if data.get("path") else ""
        return ToolCall(name=name, title=f"{data.get('pattern') or ''}{where}".strip(), output_limit=READ_OUTPUT_LIMIT)
    if lowered == "websearch":
        return ToolCall(name=name, title=str(data.get("query") or ""), output_limit=READ_OUTPUT_LIMIT)
    if lowered == "webfetch":
        return ToolCall(name=name, title=str(data.get("url") or ""), body=str(data.get("prompt") or ""), output_limit=READ_OUTPUT_LIMIT)
    if lowered in {"task", "agent"}:
        label = data.get("subagent_type") or "agent"
        return ToolCall(name=name, title=f"{label}: {data.get('description') or ''}".rstrip(": "), body=str(data.get("prompt") or ""), body_limit=2000)
    if lowered == "todowrite":
        todos = data.get("todos") or []
        return plan_call([{"status": t.get("status"), "step": t.get("content")} for t in todos if isinstance(t, dict)], name=name)
    return generic_call(name, data)


def output_text(value: Any) -> str:
    """Flatten a tool result payload (string, JSON string, or content blocks) to text."""
    if value is None:
        return ""
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith("{") and stripped.endswith("}"):
            try:
                parsed = json.loads(stripped)
            except json.JSONDecodeError:
                return value
            if isinstance(parsed, dict) and "output" in parsed:
                return str(parsed.get("output") or "")
        return value
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, dict) and item.get("type") in {"image", "input_image"}:
                parts.append("[image output omitted]")
            else:
                parts.append(output_text(item))
        return "\n".join(part for part in parts if part)
    if isinstance(value, dict):
        for key in ("output", "text", "content", "stdout", "result"):
            if key in value and value[key] not in (None, ""):
                return output_text(value[key])
        return json.dumps(value, ensure_ascii=False)
    return text_from_any(value)


def output_exit_code(value: Any) -> int | None:
    """Exit code from a Codex function output (JSON metadata or 'Process exited with code N')."""
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith("{"):
            try:
                parsed = json.loads(stripped)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, dict):
                metadata = parsed.get("metadata")
                if isinstance(metadata, dict) and isinstance(metadata.get("exit_code"), int):
                    return metadata["exit_code"]
        marker = "Process exited with code "
        if marker in value:
            tail = value.split(marker, 1)[1].split()
            if tail and tail[0].lstrip("-").isdigit():
                return int(tail[0])
    return None


_EXEC_PREAMBLE_PREFIXES = (
    "Chunk ID:",
    "Wall time",
    "Process exited with code",
    "Process running with session ID",
    "Original token count:",
    "Total output lines:",
    "Exit code:",
    "Duration:",
    "Script completed",
)


def strip_exec_preamble(text: str) -> str:
    """Drop Codex exec bookkeeping lines ("Chunk ID", "Wall time", ...) that precede ``Output:``."""
    lines = str(text or "").split("\n")
    for index, line in enumerate(lines[:12]):
        if line.strip() == "Output:":
            head = lines[:index]
            if all(not item.strip() or item.startswith(_EXEC_PREAMBLE_PREFIXES) for item in head):
                return "\n".join(lines[index + 1 :])
            break
    return str(text or "")


def _replacement_diff(old: Any, new: Any, replace_all: bool) -> str:
    old_lines = str(old or "").split("\n") if old else []
    new_lines = str(new or "").split("\n") if new else []
    lines = [f"-{line}" for line in old_lines] + [f"+{line}" for line in new_lines]
    if replace_all:
        lines.insert(0, "(replace all occurrences)")
    return "\n".join(lines)


def _parse_json_maybe(value: Any) -> Any:
    if isinstance(value, str):
        stripped = value.strip()
        if stripped[:1] in {"{", "["}:
            try:
                return json.loads(stripped)
            except json.JSONDecodeError:
                return value
    return value


def _compact_args(args: Any) -> str:
    if args in (None, "", {}, []):
        return ""
    if isinstance(args, dict):
        if len(args) == 1:
            value = next(iter(args.values()))
            if isinstance(value, str):
                return value
        return json.dumps(args, ensure_ascii=False, indent=None)
    if isinstance(args, str):
        return args
    return json.dumps(args, ensure_ascii=False)
