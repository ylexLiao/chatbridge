from __future__ import annotations

import re

from .models import Session
from .util import parse_timestamp, redact, sanitize_embedded_images


LEVEL_LIMITS = {"brief": 4, "normal": 10, "full": 40}
TOOL_LIMITS = {"brief": 5, "normal": 12, "full": 30}

HANDOFF_CAVEAT = (
    "This is session context imported from another AI tool. Verify the current workspace state first; do not blindly trust this historical summary. "
    "这是从另一个 AI 工具导入的会话上下文。请先验证当前工作区状态，不要盲信历史摘要。"
)
HANDOFF_NEXT_STEP = (
    "Before continuing this task, check the current repo state, recent file changes, and runnable tests; treat this summary as leads, not facts. "
    "继续这个任务前，先检查当前 repo 状态、最近文件变更和可运行测试；把本摘要当线索，不当事实。"
)

_TOOL_HEADER_RE = re.compile(r"^\[Tool: ([^\]]+)\] ?(.*)$")
_FILE_TITLE_RE = re.compile(r"^(?:edit|write|edit notebook) (.+)$")


def build_handoff(session: Session, target: str, level: str = "normal") -> str:
    target_label = {"codex": "Codex", "claude": "Claude Code", "copilot": "Copilot"}.get(target, target.title())
    source_label = session.source_label
    limit = LEVEL_LIMITS.get(level, LEVEL_LIMITS["normal"])
    conversation = [message for message in session.messages if message.role != "tool"]
    messages = conversation[-limit:]
    tool_messages = [message for message in session.messages if message.role == "tool"]
    artifacts = session.artifacts[-min(limit, 8):]

    lines = [
        f"[Handoff: {source_label} -> {target_label}]",
        "",
        HANDOFF_CAVEAT,
        "",
        "## Source",
        f"- Tool: {source_label}",
        f"- Session: {session.session_id}",
        f"- Title: {session.title}",
    ]
    if session.project_path:
        lines.append(f"- Project: {session.project_path}")
    if session.updated_at or session.created_at:
        lines.append(f"- Time: {_readable_time(session.updated_at or session.created_at)}")

    lines.extend(["", "## Conversation"])
    if messages:
        for message in messages:
            text = _compact(sanitize_embedded_images(redact(message.text)), 1200 if level == "full" else 500)
            lines.append(_entry(f"- {message.role}: ", text))
    else:
        lines.append("- No message body was recovered from the source history.")

    files = touched_files(session)
    if files:
        lines.extend(["", "## Files touched"])
        lines.extend(f"- {path}" for path in files[-15:])

    if tool_messages:
        tool_limit = TOOL_LIMITS.get(level, TOOL_LIMITS["normal"])
        recent = tool_messages[-tool_limit:]
        lines.extend(["", f"## Recent tool activity (last {len(recent)} of {len(tool_messages)})"])
        for message in recent:
            lines.append(f"- {_compact(sanitize_embedded_images(redact(tool_summary(message.text))), 200)}")

    if artifacts:
        lines.extend(["", "## Artifacts"])
        for artifact in artifacts:
            text = _compact(sanitize_embedded_images(redact(artifact.text)), 1000 if level == "full" else 400)
            label = artifact.kind
            if artifact.path:
                label += f" ({artifact.path})"
            lines.append(_entry(f"- {label}: ", text))

    lines.extend([
        "",
        "## Next Step",
        HANDOFF_NEXT_STEP,
    ])
    return "\n".join(lines).rstrip() + "\n"


def tool_summary(text: str) -> str:
    """One-line "name: what" summary of a rendered tool message."""
    first = str(text or "").split("\n", 1)[0]
    match = _TOOL_HEADER_RE.match(first)
    if not match:
        return first
    return f"{match.group(1)}: {match.group(2)}".rstrip(": ")


def touched_files(session: Session) -> list[str]:
    """Files edited/written by tool calls, in first-touch order."""
    seen: dict[str, None] = {}
    for message in session.messages:
        if message.role != "tool":
            continue
        first = str(message.text or "").split("\n", 1)[0]
        match = _TOOL_HEADER_RE.match(first)
        title = match.group(2) if match else ""
        file_match = _FILE_TITLE_RE.match(title)
        if not file_match:
            continue
        target = file_match.group(1).strip()
        if target.startswith(("(", "patch")):
            continue
        if re.match(r"^\d+ files: ", target):
            for line in str(message.text).split("\n")[1:]:
                for prefix in ("Added ", "Updated ", "Deleted ", "Changed ", "*** Add File: ", "*** Update File: ", "*** Delete File: "):
                    if line.startswith(prefix):
                        seen.setdefault(line[len(prefix):].split(" -> ")[0].strip(), None)
            continue
        seen.setdefault(target, None)
    return list(seen)


def _readable_time(value: object) -> str:
    parsed = parse_timestamp(value)
    return parsed.astimezone().strftime("%Y-%m-%d %H:%M %Z").strip() if parsed else str(value)


def _entry(prefix: str, text: str) -> str:
    body = text.split("\n")
    rendered = [f"{prefix}{body[0]}"]
    rendered.extend(f"  {line}" for line in body[1:])
    return "\n".join(rendered)


def _compact(text: str, limit: int) -> str:
    lines = [" ".join(line.split()) for line in text.splitlines()]
    clean = "\n".join(line for line in lines if line)
    if len(clean) <= limit:
        return clean
    return clean[: limit - 3] + "..."
