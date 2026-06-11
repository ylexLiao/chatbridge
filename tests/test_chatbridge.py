import hashlib
import base64
import json
import os
import subprocess
import sqlite3
import sys
import tempfile
import unittest
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
COPILOT_INDEX_KEY = "chat.ChatSessionStore.index"
COPILOT_AGENT_CACHE_KEY = "agentSessions.model.cache"
COPILOT_AGENT_STATE_KEY = "agentSessions.state.cache"


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def write_jsonl(path: Path, rows: list[object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
        encoding="utf-8",
    )


def read_copilot_index(workspace: Path) -> dict:
    return read_vscode_json_key(workspace, COPILOT_INDEX_KEY)


def read_vscode_json_key(workspace: Path, key: str) -> object:
    con = sqlite3.connect(workspace / "state.vscdb")
    row = con.execute("SELECT value FROM ItemTable WHERE key = ?", (key,)).fetchone()
    con.close()
    assert row is not None
    value = row[0].decode("utf-8") if isinstance(row[0], bytes) else row[0]
    return json.loads(value)


def vscode_local_session_uri(session_id: str) -> str:
    encoded = base64.urlsafe_b64encode(session_id.encode("utf-8")).decode("ascii").rstrip("=")
    return f"vscode-chat-session://local/{encoded}"


def make_home(tmp_path: Path) -> Path:
    home = tmp_path / "home"

    copilot_ws = home / ".config/Code/User/workspaceStorage/ws1"
    write_json(copilot_ws / "workspace.json", {"folder": "file:///repo/app"})
    write_json(
        copilot_ws / "chatSessions/copilot-json.json",
        {
            "sessionId": "copilot-json",
            "customTitle": "Fix auth",
            "creationDate": 1700000000000,
            "lastMessageDate": 1700000005000,
            "responderUsername": "GitHub Copilot",
            "requesterUsername": "dev",
            "requests": [
                {
                    "timestamp": 1700000001000,
                    "message": {"text": "Fix login error password:topsecret123"},
                    "response": [{"value": "Update auth handler"}],
                }
            ],
        },
    )
    write_jsonl(
        copilot_ws / "chatSessions/copilot-jsonl.jsonl",
        [
            {
                "kind": 0,
                "v": {
                    "version": 3,
                    "sessionId": "copilot-jsonl",
                    "customTitle": "Patch bug",
                    "creationDate": 1700000010000,
                    "lastMessageDate": 1700000015000,
                    "responderUsername": "GitHub Copilot",
                    "requests": [],
                },
            },
            {"kind": 1, "k": ["requests"], "v": [{"message": {"text": "Patch bug"}, "response": [{"value": "Patched"}]}]},
        ],
    )
    write_jsonl(
        copilot_ws / "chatSessions/copilot-deep-jsonl.jsonl",
        [
            {
                "kind": 0,
                "v": {
                    "version": 3,
                    "sessionId": "copilot-deep-jsonl",
                    "customTitle": "Deep Copilot Session",
                    "creationDate": 1700000040000,
                    "lastMessageDate": 1700000045000,
                    "requests": [
                        {
                            "requestId": "startup",
                            "timestamp": 1700000040000,
                            "message": {"text": "Analyze project"},
                            "response": [{"kind": "mcpServersStarting", "didStartServerIds": []}],
                        }
                    ],
                },
            },
            {
                "kind": 2,
                "k": ["requests"],
                "v": [
                    {
                        "requestId": "real-request",
                        "timestamp": 1700000041000,
                        "message": {"text": "Build the finance notebook and explain it"},
                        "response": [{"value": "Working on project files."}],
                    }
                ],
            },
            {"kind": 2, "k": ["requests", 1, "response"], "v": [{"value": "Working on project files."}]},
            {
                "kind": 1,
                "k": ["requests", 1, "result"],
                "v": {
                    "details": "Claude Opus 4.8",
                    "metadata": {
                        "renderedUserMessage": [{"text": "<context>Attached start.md explained PCA and Calendar Spread.</context>"}],
                        "toolCallRounds": [
                            {"response": "Created 讲解_PROJECT_EXPLAINED.md and explore.ipynb. Notebook 整体执行成功（292KB 输出，含所有图表，无报错）。"},
                            {"response": "Next step: add yield signal features and compare Logistic Regression with RandomForest."},
                        ],
                        "toolCallResults": {
                            "tool-1": {"content": [{"value": "The following files were successfully edited:\n/repo/app/讲解_PROJECT_EXPLAINED.md"}]},
                            "tool-2": {"content": [{"value": "File created at /repo/app/explore.ipynb"}]},
                        },
                    },
                },
            },
        ],
    )
    write_jsonl(
        copilot_ws / "GitHub.copilot-chat/transcripts/copilot-jsonl.jsonl",
        [{"role": "user", "content": "transcript request"}, {"role": "assistant", "content": "transcript answer"}],
    )
    resource = copilot_ws / "GitHub.copilot-chat/chat-session-resources/copilot-jsonl/toolu/content.txt"
    resource.parent.mkdir(parents=True, exist_ok=True)
    resource.write_text("tool output with TOKEN=ghp_secretvalue\n", encoding="utf-8")

    codex_home = home / ".codex"
    write_jsonl(
        codex_home / "session_index.jsonl",
        [{"id": "codex-1", "thread_name": "Debug server", "updated_at": "2026-01-01T00:00:00Z"}],
    )
    write_jsonl(codex_home / "history.jsonl", [{"session_id": "codex-1", "ts": 1700000020, "text": "Run tests"}])
    write_jsonl(
        codex_home / "sessions/2026/01/01/rollout-2026-01-01T00-00-00-codex-1.jsonl",
        [
            {"type": "message", "role": "user", "content": "Run tests"},
            {"type": "message", "role": "assistant", "content": "Tests failed"},
        ],
    )
    con = sqlite3.connect(codex_home / "state_5.sqlite")
    con.execute("""
        CREATE TABLE threads (
            id TEXT PRIMARY KEY, rollout_path TEXT NOT NULL, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
            source TEXT NOT NULL, model_provider TEXT NOT NULL, cwd TEXT NOT NULL, title TEXT NOT NULL,
            sandbox_policy TEXT NOT NULL, approval_mode TEXT NOT NULL, tokens_used INTEGER NOT NULL DEFAULT 0,
            has_user_event INTEGER NOT NULL DEFAULT 0, archived INTEGER NOT NULL DEFAULT 0, archived_at INTEGER,
            git_sha TEXT, git_branch TEXT, git_origin_url TEXT, cli_version TEXT NOT NULL DEFAULT '',
            first_user_message TEXT NOT NULL DEFAULT '', agent_nickname TEXT, agent_role TEXT,
            memory_mode TEXT NOT NULL DEFAULT 'enabled', model TEXT, reasoning_effort TEXT, agent_path TEXT,
            created_at_ms INTEGER, updated_at_ms INTEGER, thread_source TEXT, preview TEXT NOT NULL DEFAULT ''
        )
    """)
    con.execute(
        """
        INSERT INTO threads (
            id, rollout_path, created_at, updated_at, source, model_provider, cwd, title,
            sandbox_policy, approval_mode, tokens_used, has_user_event, archived, cli_version,
            first_user_message, memory_mode, model, reasoning_effort, created_at_ms, updated_at_ms,
            thread_source, preview
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "native-codex-1",
            str(codex_home / "sessions/2026/01/01/rollout-2026-01-01T00-00-00-native-codex-1.jsonl"),
            1700000100,
            1700000100,
            "cli",
            "codex",
            "/repo/app",
            "Native Codex",
            "workspace-write",
            "on-request",
            0,
            0,
            0,
            "0.137.0",
            "Native message",
            "enabled",
            "gpt-5.5",
            "medium",
            1700000100000,
            1700000100000,
            "user",
            "Native Codex",
        ),
    )
    con.commit()
    con.close()

    claude_home = home / ".claude"
    write_jsonl(
        claude_home / "history.jsonl",
        [{"display": "Explain repo", "timestamp": 1700000030000, "project": "/repo/app", "sessionId": "claude-1"}],
    )
    write_jsonl(
        claude_home / "projects/-repo-app/claude-1.jsonl",
        [
            {"type": "user", "message": {"content": "Explain repo"}, "timestamp": "2026-01-01T00:00:01Z", "cwd": "/repo/app"},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "Repo explained"}]}},
        ],
    )
    return home


def make_fake_rust_tui(path: Path) -> Path:
    lines = [
        "ChatBridge TUI",
        "Rust ratatui TUI",
        "GitHub Copilot",
        "Codex CLI",
        "Claude Code",
        "Prompt Handoff",
        "Native Import",
        "↑/↓ move  Enter select",
    ]
    if os.name == "nt":
        script = path.with_suffix(".py")
        script.write_text(
            "\n".join(["import sys"] + [f"print({line!r})" for line in lines] + [""]),
            encoding="utf-8",
        )
        cmd = path.with_suffix(".cmd")
        cmd.write_text(f'@echo off\r\n"{sys.executable}" "{script}" %*\r\n', encoding="utf-8")
        return cmd
    path.write_text(
        "\n".join(["#!/bin/sh"] + [f"printf '%s\\n' '{line}'" for line in lines] + [""]),
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def run_cli(home: Path, *args: str, extra_env: dict[str, str] | None = None, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["HOME"] = str(home)
    env["PYTHONPATH"] = str(ROOT)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, "-m", "chatbridge", *args],
        cwd=cwd or ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


class ChatbridgeTests(unittest.TestCase):
    def test_list_sources_reads_three_tools(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            home = make_home(Path(temp_dir))

            result = run_cli(home, "list", "--source", "copilot")

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("copilot-json", result.stdout)
            self.assertIn("/repo/app", result.stdout)

    def test_handoff_redacts_and_mentions_source_and_target(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            home = make_home(Path(temp_dir))

            result = run_cli(home, "handoff", "--from", "copilot", "--to", "codex", "--session", "copilot-jsonl", "--level", "full")

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("[Handoff: Copilot -> Codex]", result.stdout)
            self.assertIn("Patch bug", result.stdout)
            self.assertIn("transcript request", result.stdout)
            self.assertIn("TOKEN=[REDACTED]", result.stdout)
            self.assertNotIn("ghp_secretvalue", result.stdout)

    def test_handoff_recovers_deep_copilot_jsonl_deltas(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            home = make_home(Path(temp_dir))

            result = run_cli(home, "handoff", "--from", "copilot", "--to", "codex", "--session", "copilot-deep-jsonl", "--level", "full")

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Build the finance notebook and explain it", result.stdout)
            self.assertIn("讲解_PROJECT_EXPLAINED.md", result.stdout)
            self.assertIn("explore.ipynb", result.stdout)
            self.assertIn("Notebook 整体执行成功", result.stdout)
            self.assertIn("292KB", result.stdout)
            self.assertIn("RandomForest", result.stdout)
            self.assertNotIn("mcpServersStarting", result.stdout)

    def test_redact_handles_password_environment_variables(self) -> None:
        from chatbridge.util import redact

        text = redact("export PASS='super-secret' PWD=another-secret token=abc123456789")

        self.assertNotIn("super-secret", text)
        self.assertNotIn("another-secret", text)
        self.assertNotIn("abc123456789", text)
        self.assertIn("PASS='[REDACTED]'", text)
        self.assertIn("PWD=[REDACTED]", text)
        self.assertIn("token=[REDACTED]", text)

    def test_file_uri_to_path_decodes_cross_platform_paths(self) -> None:
        from chatbridge.util import file_uri_to_path

        self.assertEqual(file_uri_to_path("file:///home/dev/My%20Project"), "/home/dev/My Project")
        self.assertEqual(file_uri_to_path("file:///Users/dev/%E6%B5%8B%E8%AF%95"), "/Users/dev/测试")
        self.assertEqual(file_uri_to_path("file:///C:/Users/Alice/My%20Project"), "C:/Users/Alice/My Project")
        self.assertEqual(file_uri_to_path("file:///C|/Users/Alice/My%20Project"), "C:/Users/Alice/My Project")
        self.assertEqual(file_uri_to_path("file://server/share/My%20Project"), "//server/share/My Project")
        self.assertEqual(file_uri_to_path("file://localhost/C:/Users/Alice/My%20Project"), "C:/Users/Alice/My Project")
        self.assertEqual(file_uri_to_path("vscode-remote://ssh-remote%2Bhost/home/dev/app"), "vscode-remote://ssh-remote%2Bhost/home/dev/app")

    def test_codex_import_start_time_uses_local_timezone(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            home = make_home(Path(temp_dir))

            result = run_cli(
                home,
                "native-import",
                "--from",
                "copilot",
                "--to",
                "codex",
                "--session",
                "copilot-json",
                "--apply",
                extra_env={"TZ": "Asia/Hong_Kong"},
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            session_index_text = (home / ".codex/session_index.jsonl").read_text(encoding="utf-8")
            self.assertIn("Copilot started 2023-11-15 06:13", session_index_text)
            self.assertNotIn("Copilot started 2023-11-14 22:13 UTC", session_index_text)

    def test_codex_import_assistant_context_keeps_single_marker(self) -> None:
        from chatbridge.writers import _codex_import_assistant_context

        legacy_marker = "Imported by ChatNBridge. Treat this as context, not verified fact."
        marker = "Imported by ChatBridge. Treat this as context, not verified fact."
        context = _codex_import_assistant_context(f"[Handoff: Copilot -> Codex]\n{legacy_marker}\nNested old context.\n{legacy_marker}")

        self.assertEqual(context.count(marker), 1)
        self.assertNotIn(legacy_marker, context)
        self.assertTrue(context.endswith(marker))

    def test_claude_slug_matches_native_project_directory(self) -> None:
        from chatbridge.util import project_to_claude_slug

        self.assertEqual(
            project_to_claude_slug("/home/dev/Desktop/Financial_Derivative"),
            "-home-dev-Desktop-Financial-Derivative",
        )
        self.assertEqual(
            project_to_claude_slug("file:///Users/dev/My%20Project"),
            "-Users-dev-My-Project",
        )

    def test_native_import_dry_run_does_not_write(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            home = make_home(Path(temp_dir))
            before = sorted((home / ".codex/sessions").rglob("*.jsonl"))

            result = run_cli(home, "native-import", "--from", "copilot", "--to", "codex", "--session", "copilot-json", "--dry-run")

            after = sorted((home / ".codex/sessions").rglob("*.jsonl"))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("DRY RUN", result.stdout)
            self.assertEqual(before, after)

    def test_native_import_apply_writes_codex_session_and_backup(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            home = make_home(Path(temp_dir))
            con = sqlite3.connect(home / ".codex/state_5.sqlite")
            con.execute(
                """
                INSERT INTO threads (
                    id, rollout_path, created_at, updated_at, source, model_provider, cwd, title,
                    sandbox_policy, approval_mode, tokens_used, has_user_event, archived, cli_version,
                    first_user_message, memory_mode, model, reasoning_effort, created_at_ms, updated_at_ms,
                    thread_source, preview
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "polluted-import",
                    str(home / ".codex/sessions/2026/01/01/rollout-2026-01-01T00-00-01-imported-polluted-import.jsonl"),
                    1800000000,
                    1800000000,
                    "cli",
                    "codex",
                    "/repo/app",
                    "[Imported from Copilot] Polluted",
                    '{"type":"read-only"}',
                    "on-request",
                    0,
                    0,
                    0,
                    "bad-import-version",
                    "[Imported from Copilot] Polluted",
                    "enabled",
                    "chatbridge-import",
                    "low",
                    1800000000000,
                    1800000000000,
                    "user",
                    "[Imported from Copilot] Polluted",
                ),
            )
            con.commit()
            con.close()

            result = run_cli(
                home,
                "native-import",
                "--from",
                "copilot",
                "--to",
                "codex",
                "--session",
                "copilot-json",
                "--apply",
                extra_env={"TZ": "Asia/Hong_Kong"},
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            session_file = next(Path(line.split("Session file: ", 1)[1]) for line in result.stdout.splitlines() if line.startswith("Session file: "))
            self.assertTrue(session_file.exists())
            rows = [json.loads(line) for line in session_file.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(rows[0]["type"], "session_meta")
            self.assertEqual(rows[0]["payload"]["source"], "cli")
            self.assertEqual(rows[0]["payload"]["thread_source"], "user")
            self.assertEqual(rows[0]["payload"]["model_provider"], "codex")
            self.assertEqual(rows[0]["payload"]["cli_version"], "0.137.0")
            response_messages = [row["payload"] for row in rows if row.get("type") == "response_item" and row.get("payload", {}).get("type") == "message"]
            user_texts = [msg["content"][0]["text"] for msg in response_messages if msg.get("role") == "user"]
            assistant_texts = [msg["content"][0]["text"] for msg in response_messages if msg.get("role") == "assistant"]
            self.assertIn("[Imported from Copilot] Fix auth", user_texts)
            self.assertIn("Fix login error password:[REDACTED]", user_texts)
            self.assertIn("Update auth handler", assistant_texts)
            self.assertTrue(any("[Copilot last query]" in text and "Fix login error" in text for text in user_texts))
            self.assertTrue(any("[Copilot last reply]" in text and "Update auth handler" in text for text in assistant_texts))
            assistant_context = next(text for text in assistant_texts if "[Handoff: Copilot -> Codex]" in text)
            self.assertIn("Fix login error", assistant_context)
            rollout_text = session_file.read_text(encoding="utf-8")
            self.assertNotIn("topsecret123", rollout_text)
            self.assertIn("password:[REDACTED]", rollout_text)
            event_payloads = [row["payload"] for row in rows if row.get("type") == "event_msg"]
            event_types = {payload.get("type") for payload in event_payloads}
            self.assertIn("task_started", event_types)
            self.assertIn("task_complete", event_types)
            self.assertIn("token_count", event_types)
            self.assertTrue(any(payload.get("message") == "<EXTERNAL SESSION IMPORTED>" for payload in event_payloads))
            self.assertTrue(any(payload.get("type") == "user_message" and "[Copilot last query]" in payload.get("message", "") for payload in event_payloads))
            self.assertTrue(any(payload.get("type") == "agent_message" and "[Copilot last reply]" in payload.get("message", "") for payload in event_payloads))
            self.assertEqual(rows[-1]["type"], "turn_context")
            self.assertEqual(rows[-1]["payload"]["model"], "gpt-5.5")
            session_index_text = (home / ".codex/session_index.jsonl").read_text(encoding="utf-8")
            self.assertIn("[Imported from Copilot]", session_index_text)
            self.assertIn("Copilot started 2023-11-15 06:13", session_index_text)
            con = sqlite3.connect(home / ".codex/state_5.sqlite")
            db_rows = con.execute(
                """
                SELECT id, title, rollout_path, source, thread_source, model_provider, model, cli_version, has_user_event, first_user_message
                FROM threads
                WHERE title LIKE '[Imported from Copilot] Fix auth%'
                """
            ).fetchall()
            con.close()
            self.assertEqual(len(db_rows), 1)
            self.assertEqual(uuid.UUID(db_rows[0][0]).version, 7)
            self.assertNotIn("-imported-", Path(db_rows[0][2]).name)
            self.assertEqual(db_rows[0][3], "cli")
            self.assertEqual(db_rows[0][4], "user")
            self.assertEqual(db_rows[0][5], "codex")
            self.assertEqual(db_rows[0][6], "gpt-5.5")
            self.assertEqual(db_rows[0][7], "0.137.0")
            self.assertEqual(db_rows[0][8], 0)
            self.assertIn("Copilot started 2023-11-15 06:13", db_rows[0][1])
            self.assertEqual(db_rows[0][9], "[Imported from Copilot] Fix auth")
            backups = list((home / ".chatbridge/backups").rglob("*"))
            self.assertTrue(backups)

    def test_codex_import_uses_existing_sqlite_columns_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            home = make_home(Path(temp_dir))
            db = home / ".codex/state_5.sqlite"
            con = sqlite3.connect(db)
            con.execute("DROP TABLE threads")
            con.execute(
                """
                CREATE TABLE threads (
                    id TEXT PRIMARY KEY,
                    rollout_path TEXT NOT NULL,
                    title TEXT NOT NULL,
                    cwd TEXT NOT NULL,
                    updated_at INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            con.commit()
            con.close()

            result = run_cli(home, "native-import", "--from", "copilot", "--to", "codex", "--session", "copilot-json", "--apply")

            self.assertEqual(result.returncode, 0, result.stderr)
            con = sqlite3.connect(db)
            row = con.execute("SELECT id, title, cwd, updated_at FROM threads WHERE title LIKE '[Imported from Copilot] Fix auth%'").fetchone()
            con.close()
            self.assertIsNotNone(row)
            self.assertEqual(uuid.UUID(row[0]).version, 7)
            self.assertEqual(row[2], "/repo/app")
            self.assertGreater(row[3], 0)

    def test_native_import_duplicate_codex_requires_confirmation_and_suffixes_copy(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            home = make_home(Path(temp_dir))

            first = run_cli(home, "native-import", "--from", "copilot", "--to", "codex", "--session", "copilot-json", "--apply")
            duplicate = run_cli(home, "native-import", "--from", "copilot", "--to", "codex", "--session", "copilot-json", "--apply")
            allowed = run_cli(home, "native-import", "--from", "copilot", "--to", "codex", "--session", "copilot-json", "--apply", "--allow-duplicate")

            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual(duplicate.returncode, 2)
            self.assertIn("Duplicate native import", duplicate.stderr)
            self.assertEqual(allowed.returncode, 0, allowed.stderr)
            session_index_text = (home / ".codex/session_index.jsonl").read_text(encoding="utf-8")
            self.assertIn("[Imported from Copilot] Fix auth (1) · Copilot started", session_index_text)

    def test_api_sessions_returns_stable_json_shape(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            home = make_home(Path(temp_dir))

            result = run_cli(home, "api", "sessions", "--source", "copilot", "--limit", "3")

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertTrue(payload["ok"])
            data = payload["data"]
            rows = data["sessions"]
            self.assertTrue(rows)
            self.assertEqual(data["limit"], 3)
            self.assertEqual(data["loaded"], len(rows))
            self.assertGreaterEqual(data["total"], data["loaded"])
            self.assertEqual(data["hasMore"], data["total"] > data["loaded"])
            row = rows[0]
            self.assertIn("source", row)
            self.assertIn("sessionId", row)
            self.assertIn("title", row)
            self.assertIn("projectPath", row)
            self.assertIn("createdAt", row)
            self.assertIn("updatedAt", row)
            self.assertIn("rawPath", row)
            self.assertIn("scope", row)
            self.assertEqual(row["source"], "copilot")
            self.assertIn(row["scope"], {"LOCAL", "REMOTE"})
            self.assertLessEqual(len(rows), 3)

    def test_api_sessions_uses_copilot_limited_fast_path(self) -> None:
        import argparse
        import contextlib
        import io
        from unittest.mock import patch

        from chatbridge.api import handle_api
        from chatbridge.models import Session

        calls: list[tuple[str, bool, int | None]] = []

        def fake_load_sessions(source: str, home: Path, metadata_only: bool = False, limit: int | None = None) -> list[Session]:
            calls.append((source, metadata_only, limit))
            return [
                Session(
                    source="copilot",
                    session_id=f"session-{index}",
                    title=f"Session {index}",
                    project_path="/repo/app",
                    updated_at=1700000000000 + index,
                    raw_path=Path(f"/tmp/session-{index}.json"),
                )
                for index in range(50)
            ]

        args = argparse.Namespace(api_command="sessions", source="copilot", limit=50, project=None)
        output = io.StringIO()
        with patch("chatbridge.api.load_sessions", side_effect=fake_load_sessions), patch("chatbridge.api.count_sessions", return_value=100):
            with contextlib.redirect_stdout(output):
                code = handle_api(args, Path("/tmp/home"))

        self.assertEqual(code, 0)
        self.assertEqual(calls, [("copilot", True, 50)])
        payload = json.loads(output.getvalue())
        self.assertTrue(payload["data"]["hasMore"])
        self.assertEqual(payload["data"]["total"], 100)

    def test_api_sessions_uses_limited_fast_path_for_codex_and_claude(self) -> None:
        import argparse
        import contextlib
        import io
        from unittest.mock import patch

        from chatbridge.api import handle_api
        from chatbridge.models import Session

        for source in ("codex", "claude"):
            calls: list[tuple[str, bool, int | None]] = []

            def fake_load_sessions(source_arg: str, home: Path, metadata_only: bool = False, limit: int | None = None) -> list[Session]:
                calls.append((source_arg, metadata_only, limit))
                return [Session(source=source_arg, session_id="s1", title="Session", updated_at=1)]

            args = argparse.Namespace(api_command="sessions", source=source, limit=50, project=None)
            output = io.StringIO()
            with patch("chatbridge.api.load_sessions", side_effect=fake_load_sessions), patch("chatbridge.api.count_sessions", return_value=1):
                with contextlib.redirect_stdout(output):
                    code = handle_api(args, Path("/tmp/home"))

            self.assertEqual(code, 0)
            self.assertEqual(calls, [(source, True, 50)])
            self.assertTrue(json.loads(output.getvalue())["ok"])

    def test_copilot_metadata_fast_reads_only_file_prefix(self) -> None:
        from unittest.mock import patch

        from chatbridge.parsers import _copilot_metadata_fast

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "session-prefix.json"
            path.write_text(
                '{"sessionId":"session-prefix","customTitle":"Prefix Only","creationDate":1700000000000,"lastMessageDate":1700000001000}'
                + (" " * 1024),
                encoding="utf-8",
            )

            with patch.object(Path, "read_bytes", side_effect=AssertionError("read_bytes would load the whole Copilot chat file")):
                session = _copilot_metadata_fast(path, "/repo/app", "workspace")

        self.assertIsNotNone(session)
        self.assertEqual(session.session_id, "session-prefix")
        self.assertEqual(session.title, "Prefix Only")

    def test_api_native_import_duplicate_returns_json_error_without_stderr(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            home = make_home(Path(temp_dir))

            first = run_cli(home, "native-import", "--from", "copilot", "--to", "codex", "--session", "copilot-json", "--apply")
            duplicate = run_cli(home, "api", "native-import", "--from", "copilot", "--to", "codex", "--session", "copilot-json", "--apply")

            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual(duplicate.returncode, 0, duplicate.stderr)
            self.assertEqual(duplicate.stderr, "")
            payload = json.loads(duplicate.stdout)
            self.assertFalse(payload["ok"])
            self.assertEqual(payload["kind"], "duplicate")
            self.assertIn("Duplicate native import", payload["message"])
            self.assertIn("(1)", payload["nextTitle"])

    def test_native_import_apply_writes_claude_project_session(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            home = make_home(Path(temp_dir))

            result = run_cli(
                home,
                "native-import",
                "--from",
                "copilot",
                "--to",
                "claude",
                "--session",
                "copilot-json",
                "--project",
                "/home/dev/Desktop/Financial_Derivative",
                "--apply",
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            imported = list((home / ".claude/projects/-home-dev-Desktop-Financial-Derivative").glob("*.jsonl"))
            self.assertEqual(len(imported), 1)
            sid = imported[0].stem
            self.assertEqual(str(uuid.UUID(sid)), sid)
            rows = [json.loads(line) for line in imported[0].read_text(encoding="utf-8").splitlines()]
            message_rows = [row for row in rows if row.get("type") in {"user", "assistant"}]
            self.assertTrue(message_rows)
            for row in message_rows:
                self.assertEqual(row.get("sessionId"), sid)
                self.assertEqual(row.get("cwd"), "/home/dev/Desktop/Financial_Derivative")
                self.assertIn("uuid", row)
                self.assertIn("parentUuid", row)
                self.assertFalse(row.get("isSidechain"))
                self.assertEqual(row.get("userType"), "external")
                self.assertEqual(row.get("entrypoint"), "cli")
                self.assertIn("role", row.get("message", {}))
            self.assertEqual(message_rows[0]["message"]["role"], "user")
            self.assertEqual(message_rows[-1]["message"]["role"], "assistant")
            self.assertIsNone(message_rows[0]["parentUuid"])
            for previous, current in zip(message_rows, message_rows[1:]):
                self.assertEqual(current["parentUuid"], previous["uuid"])
            transcript_text = json.dumps(message_rows, ensure_ascii=False)
            self.assertIn("Fix login error password:[REDACTED]", transcript_text)
            self.assertIn("Update auth handler", transcript_text)
            self.assertNotIn("topsecret123", transcript_text)
            self.assertTrue(any(row.get("type") == "last-prompt" and row.get("sessionId") == sid and "Copilot started" in row.get("lastPrompt", "") for row in rows))
            history_text = (home / ".claude/history.jsonl").read_text(encoding="utf-8")
            self.assertIn("[Imported from Copilot]", history_text)
            self.assertIn("Copilot started", history_text)
            self.assertIn(f'"sessionId": "{sid}"', history_text)

    def test_native_import_duplicate_claude_requires_confirmation_and_suffixes_copy(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            home = make_home(Path(temp_dir))
            project = "/home/dev/Desktop/Financial_Derivative"

            first = run_cli(home, "native-import", "--from", "copilot", "--to", "claude", "--session", "copilot-json", "--project", project, "--apply")
            duplicate = run_cli(home, "native-import", "--from", "copilot", "--to", "claude", "--session", "copilot-json", "--project", project, "--apply")
            allowed = run_cli(home, "native-import", "--from", "copilot", "--to", "claude", "--session", "copilot-json", "--project", project, "--apply", "--allow-duplicate")

            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual(duplicate.returncode, 2)
            self.assertIn("Duplicate native import", duplicate.stderr)
            self.assertEqual(allowed.returncode, 0, allowed.stderr)
            imported = sorted((home / ".claude/projects/-home-dev-Desktop-Financial-Derivative").glob("*.jsonl"))
            self.assertEqual(len(imported), 2)
            prompts = []
            for path in imported:
                rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
                prompts.extend(row.get("lastPrompt", "") for row in rows if row.get("type") == "last-prompt")
            self.assertTrue(any("[Imported from Copilot] Fix auth (1) · Copilot started" in prompt for prompt in prompts))

    def test_repair_claude_imports_moves_old_imports_to_native_shape(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            home = make_home(Path(temp_dir))
            old_sid = "imported-copilot-old"
            old_dir = home / ".claude/projects/-home-dev-Desktop-Financial_Derivative"
            write_jsonl(
                old_dir / f"{old_sid}.jsonl",
                [
                    {"type": "user", "message": {"content": "[Handoff: Copilot -> Claude Code]\nOld handoff"}, "timestamp": "2026-06-05T00:00:00Z", "cwd": "/home/dev/Desktop/Financial_Derivative"},
                    {"type": "assistant", "message": {"content": [{"type": "text", "text": "Imported handoff context."}]}, "timestamp": "2026-06-05T00:00:01Z"},
                ],
            )
            with (home / ".claude/history.jsonl").open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({"display": "[Imported from Copilot] Old Claude Import", "pastedContents": {}, "timestamp": 1780623686705, "project": "/home/dev/Desktop/Financial_Derivative", "sessionId": old_sid}, ensure_ascii=False) + "\n")

            result = run_cli(home, "repair-claude-imports", "--apply")

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Repaired 1 Claude Code imported session", result.stdout)
            repaired = [
                path
                for path in (home / ".claude/projects/-home-dev-Desktop-Financial-Derivative").glob("*.jsonl")
                if path.stem != "claude-1"
            ]
            self.assertEqual(len(repaired), 1)
            new_sid = repaired[0].stem
            self.assertEqual(str(uuid.UUID(new_sid)), new_sid)
            rows = [json.loads(line) for line in repaired[0].read_text(encoding="utf-8").splitlines()]
            self.assertTrue(any(row.get("type") == "last-prompt" and row.get("sessionId") == new_sid for row in rows))
            self.assertTrue(all(row.get("sessionId") == new_sid for row in rows if row.get("type") in {"user", "assistant"}))
            history_rows = [json.loads(line) for line in (home / ".claude/history.jsonl").read_text(encoding="utf-8").splitlines()]
            repaired_history = [row for row in history_rows if row.get("display") == "[Imported from Copilot] Old Claude Import"]
            self.assertEqual(len(repaired_history), 1)
            self.assertEqual(repaired_history[0]["sessionId"], new_sid)

    def test_repair_claude_imports_updates_native_last_prompt_title(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            home = make_home(Path(temp_dir))
            sid = str(uuid.uuid4())
            user_uuid = str(uuid.uuid4())
            assistant_uuid = str(uuid.uuid4())
            display = "[Imported from Copilot] Fix auth · Copilot started 2023-11-15 06:13 HKT"
            project = "/repo/app"
            write_jsonl(
                home / f".claude/projects/-repo-app/{sid}.jsonl",
                [
                    {
                        "type": "user",
                        "sessionId": sid,
                        "uuid": user_uuid,
                        "parentUuid": None,
                        "message": {"role": "user", "content": "[Handoff: Copilot -> Claude Code]\n- Session: copilot-json"},
                        "timestamp": "2026-06-05T00:00:00Z",
                        "cwd": project,
                    },
                    {
                        "type": "assistant",
                        "sessionId": sid,
                        "uuid": assistant_uuid,
                        "parentUuid": user_uuid,
                        "message": {"role": "assistant", "content": [{"type": "text", "text": "Imported handoff context."}]},
                        "timestamp": "2026-06-05T00:00:01Z",
                        "cwd": project,
                    },
                    {"type": "last-prompt", "lastPrompt": "[Imported from Copilot] Fix auth", "leafUuid": assistant_uuid, "sessionId": sid},
                ],
            )
            with (home / ".claude/history.jsonl").open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({"display": display, "pastedContents": {}, "timestamp": 1780623686705, "project": project, "sessionId": sid}, ensure_ascii=False) + "\n")

            result = run_cli(home, "repair-claude-imports", "--apply")

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Repaired 1 Claude Code imported session", result.stdout)
            rows = [json.loads(line) for line in (home / f".claude/projects/-repo-app/{sid}.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertTrue(any(row.get("type") == "last-prompt" and row.get("lastPrompt") == display for row in rows))

    def test_native_import_apply_writes_copilot_chat_session(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            home = make_home(Path(temp_dir))

            result = run_cli(home, "native-import", "--from", "codex", "--to", "copilot", "--session", "codex-1", "--project", "/repo/app", "--apply")

            self.assertEqual(result.returncode, 0, result.stderr)
            session_file = next(Path(line.split("Session file: ", 1)[1]) for line in result.stdout.splitlines() if line.startswith("Session file: "))
            self.assertEqual(session_file.suffix, ".jsonl")
            mirror = session_file.with_suffix(".json")
            self.assertTrue(mirror.exists())
            payload = json.loads(mirror.read_text(encoding="utf-8"))
            self.assertTrue(payload["customTitle"].startswith("[Imported from Codex]"))
            self.assertGreaterEqual(len(payload["requests"]), 1)
            first_request = payload["requests"][0]
            self.assertEqual(first_request["message"]["parts"][0]["kind"], "text")
            self.assertIn("range", first_request["message"]["parts"][0])
            self.assertIn("editorRange", first_request["message"]["parts"][0])
            self.assertEqual(first_request["variableData"], {"variables": []})
            self.assertEqual(first_request["modelState"]["value"], 1)
            jsonl_rows = [json.loads(line) for line in session_file.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(jsonl_rows[0]["kind"], 0)
            self.assertEqual(jsonl_rows[0]["v"]["sessionId"], payload["sessionId"])
            index = read_copilot_index(home / ".config/Code/User/workspaceStorage/ws1")
            self.assertIn(payload["sessionId"], index["entries"])
            self.assertEqual(index["entries"][payload["sessionId"]]["title"], payload["customTitle"])
            self.assertFalse(index["entries"][payload["sessionId"]]["isEmpty"])
            agent_cache = read_vscode_json_key(home / ".config/Code/User/workspaceStorage/ws1", COPILOT_AGENT_CACHE_KEY)
            resource = vscode_local_session_uri(payload["sessionId"])
            self.assertTrue(any(item.get("resource") == resource and item.get("label") == payload["customTitle"] for item in agent_cache))
            agent_state = read_vscode_json_key(home / ".config/Code/User/workspaceStorage/ws1", COPILOT_AGENT_STATE_KEY)
            self.assertTrue(any(item.get("resource") == resource and item.get("archived") is False for item in agent_state))
            request_text = json.dumps(payload["requests"], ensure_ascii=False)
            self.assertIn("Run tests", request_text)
            self.assertIn("Tests failed", request_text)

    def test_native_import_to_copilot_falls_back_to_current_workspace_when_session_has_no_project(self) -> None:
        # The codex-1 fixture session has no project of its own, so the import
        # falls back to the directory ChatBridge runs from.
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            home = make_home(temp)
            current_project = temp / "current-project"
            current_project.mkdir()
            current_ws = home / ".config/Code/User/workspaceStorage/current"
            write_json(current_ws / "workspace.json", {"folder": current_project.as_uri()})

            result = run_cli(
                home,
                "native-import",
                "--from",
                "codex",
                "--to",
                "copilot",
                "--session",
                "codex-1",
                "--apply",
                cwd=current_project,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            imported = list((current_ws / "chatSessions").glob("*.json"))
            self.assertTrue(imported)
            self.assertFalse(list((home / ".config/Code/User/workspaceStorage/ws1/chatSessions").glob("*imported*.json")))
            self.assertIn(str(current_ws), result.stdout)

    def test_codex_to_claude_native_import_preserves_transcript(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            home = make_home(Path(temp_dir))

            result = run_cli(home, "native-import", "--from", "codex", "--to", "claude", "--session", "codex-1", "--project", "/repo/app", "--apply")

            self.assertEqual(result.returncode, 0, result.stderr)
            imported = [
                path
                for path in (home / ".claude/projects/-repo-app").glob("*.jsonl")
                if path.stem != "claude-1"
            ]
            self.assertEqual(len(imported), 1)
            rows = [json.loads(line) for line in imported[0].read_text(encoding="utf-8").splitlines()]
            message_rows = [row for row in rows if row.get("type") in {"user", "assistant"}]
            transcript_text = json.dumps(message_rows, ensure_ascii=False)
            self.assertIn("Run tests", transcript_text)
            self.assertIn("Tests failed", transcript_text)
            self.assertEqual(message_rows[0]["message"]["role"], "user")
            self.assertEqual(message_rows[1]["message"]["role"], "assistant")
            self.assertEqual(message_rows[1]["parentUuid"], message_rows[0]["uuid"])

    def test_native_import_to_claude_falls_back_to_current_project_when_session_has_no_project(self) -> None:
        # The codex-1 fixture session has no project of its own, so the import
        # falls back to the directory ChatBridge runs from.
        from chatbridge.util import project_to_claude_slug

        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            home = make_home(temp)
            current_project = temp / "current-project"
            current_project.mkdir()

            result = run_cli(
                home,
                "native-import",
                "--from",
                "codex",
                "--to",
                "claude",
                "--session",
                "codex-1",
                "--apply",
                cwd=current_project,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            session_file = next(Path(line.split("Session file: ", 1)[1]) for line in result.stdout.splitlines() if line.startswith("Session file: "))
            expected_project = str(current_project.resolve())
            self.assertEqual(session_file.parent.name, project_to_claude_slug(expected_project))
            rows = [json.loads(line) for line in session_file.read_text(encoding="utf-8").splitlines()]
            self.assertTrue(any(row.get("cwd") == expected_project for row in rows if isinstance(row, dict)))
            history_rows = [json.loads(line) for line in (home / ".claude/history.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertTrue(any(row.get("project") == expected_project for row in history_rows if row.get("sessionId") == session_file.stem))

    def test_codex_state_db_session_can_be_imported_by_id(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            home = make_home(Path(temp_dir))
            root = home / ".codex"
            rollout = home / "external-rollouts/state-only.jsonl"
            write_jsonl(
                rollout,
                [
                    {"type": "session_meta", "payload": {"id": "state-only", "cwd": "/repo/app", "timestamp": "2026-01-02T00:00:00Z"}},
                    {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "State DB prompt"}]}},
                    {"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "State DB answer"}]}},
                ],
            )
            con = sqlite3.connect(root / "state_5.sqlite")
            con.execute(
                """
                INSERT INTO threads (
                    id, rollout_path, created_at, updated_at, source, model_provider, cwd, title,
                    sandbox_policy, approval_mode, tokens_used, has_user_event, archived, cli_version,
                    first_user_message, memory_mode, model, reasoning_effort, created_at_ms, updated_at_ms,
                    thread_source, preview
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "state-only",
                    str(rollout),
                    1700000500,
                    1700000500,
                    "cli",
                    "codex",
                    "/repo/app",
                    "State DB Thread",
                    "workspace-write",
                    "on-request",
                    0,
                    1,
                    0,
                    "0.137.0",
                    "State DB prompt",
                    "enabled",
                    "gpt-5.5",
                    "medium",
                    1700000500000,
                    1700000500000,
                    "user",
                    "State DB preview",
                ),
            )
            con.commit()
            con.close()

            result = run_cli(
                home,
                "native-import",
                "--from",
                "codex",
                "--to",
                "claude",
                "--session",
                "state-only",
                "--project",
                "/repo/app",
                "--apply",
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            imported = [
                path
                for path in (home / ".claude/projects/-repo-app").glob("*.jsonl")
                if path.stem != "claude-1"
            ]
            self.assertEqual(len(imported), 1)
            text = imported[0].read_text(encoding="utf-8")
            self.assertIn("State DB prompt", text)
            self.assertIn("State DB answer", text)

    def test_claude_to_codex_native_import_preserves_transcript(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            home = make_home(Path(temp_dir))

            result = run_cli(home, "native-import", "--from", "claude", "--to", "codex", "--session", "claude-1", "--apply")

            self.assertEqual(result.returncode, 0, result.stderr)
            session_file = next(Path(line.split("Session file: ", 1)[1]) for line in result.stdout.splitlines() if line.startswith("Session file: "))
            rows = [json.loads(line) for line in session_file.read_text(encoding="utf-8").splitlines()]
            response_messages = [row["payload"] for row in rows if row.get("type") == "response_item" and row.get("payload", {}).get("type") == "message"]
            user_texts = [msg["content"][0]["text"] for msg in response_messages if msg.get("role") == "user"]
            assistant_texts = [msg["content"][0]["text"] for msg in response_messages if msg.get("role") == "assistant"]
            self.assertIn("Explain repo", user_texts)
            self.assertIn("Repo explained", assistant_texts)
            self.assertTrue(any(text.startswith("[Handoff: Claude Code -> Codex]") for text in assistant_texts))
            event_types = {row["payload"].get("type") for row in rows if row.get("type") == "event_msg"}
            self.assertIn("task_started", event_types)
            self.assertIn("task_complete", event_types)
            self.assertIn("token_count", event_types)

    def test_claude_to_copilot_native_import_preserves_transcript(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            home = make_home(Path(temp_dir))

            result = run_cli(home, "native-import", "--from", "claude", "--to", "copilot", "--session", "claude-1", "--project", "/repo/app", "--apply")

            self.assertEqual(result.returncode, 0, result.stderr)
            session_file = next(Path(line.split("Session file: ", 1)[1]) for line in result.stdout.splitlines() if line.startswith("Session file: "))
            payload = json.loads(session_file.with_suffix(".json").read_text(encoding="utf-8"))
            self.assertTrue(payload["customTitle"].startswith("[Imported from Claude Code]"))
            self.assertIn(payload["sessionId"], read_copilot_index(home / ".config/Code/User/workspaceStorage/ws1")["entries"])
            request_text = json.dumps(payload["requests"], ensure_ascii=False)
            self.assertIn("Explain repo", request_text)
            self.assertIn("Repo explained", request_text)

    def test_copilot_import_creates_missing_workspace_storage(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            home = make_home(Path(temp_dir))
            project = "/repo/new-project"
            result = run_cli(home, "native-import", "--from", "claude", "--to", "copilot", "--session", "claude-1", "--project", project, "--apply")

            self.assertEqual(result.returncode, 0, result.stderr)
            workspace_hash = hashlib.md5("file:///repo/new-project".encode("utf-8")).hexdigest()
            workspace = home / ".config/Code/User/workspaceStorage" / workspace_hash
            self.assertTrue((workspace / "workspace.json").exists())
            session_file = next(Path(line.split("Session file: ", 1)[1]) for line in result.stdout.splitlines() if line.startswith("Session file: "))
            self.assertTrue(session_file.exists())
            payload = json.loads(session_file.with_suffix(".json").read_text(encoding="utf-8"))
            self.assertTrue(payload["requests"])
            self.assertIn(payload["sessionId"], read_copilot_index(workspace)["entries"])

    def test_copilot_import_to_remote_project_uses_local_workspace_storage(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            home = make_home(Path(temp_dir))
            project = "vscode-remote://ssh-remote%2Bdemo/home/ubuntu/chatbridge"

            result = run_cli(home, "native-import", "--from", "claude", "--to", "copilot", "--session", "claude-1", "--project", project, "--apply")

            self.assertEqual(result.returncode, 0, result.stderr)
            workspace_hash = hashlib.md5(project.encode("utf-8")).hexdigest()
            workspace = home / ".config/Code/User/workspaceStorage" / workspace_hash
            self.assertEqual(json.loads((workspace / "workspace.json").read_text(encoding="utf-8"))["folder"], project)
            session_file = next(Path(line.split("Session file: ", 1)[1]) for line in result.stdout.splitlines() if line.startswith("Session file: "))
            self.assertEqual(session_file.parent, workspace / "chatSessions")
            payload = json.loads(session_file.with_suffix(".json").read_text(encoding="utf-8"))
            self.assertIn(payload["sessionId"], read_copilot_index(workspace)["entries"])

    def test_repair_copilot_imports_writes_jsonl_and_index(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            home = make_home(Path(temp_dir))
            workspace = home / ".config/Code/User/workspaceStorage/ws1"
            sid = "imported-codex-old"
            payload = {
                "version": 3,
                "sessionId": sid,
                "customTitle": "[Imported from Codex] Old import",
                "creationDate": 1700000100000,
                "lastMessageDate": 1700000200000,
                "initialLocation": "panel",
                "isImported": True,
                "requests": [
                    {
                        "requestId": "bootstrap",
                        "timestamp": 1700000090000,
                        "message": {"text": "Imported context from Codex", "parts": [{"text": "Imported context from Codex"}]},
                        "response": [{"value": "<permissions instructions>\nFilesystem sandboxing defines which files can be read."}],
                    },
                    {
                        "requestId": "r1",
                        "timestamp": 1700000100000,
                        "message": {"text": "Old prompt", "parts": [{"text": "Old prompt"}]},
                        "response": [{"value": "Old answer"}],
                    }
                ],
            }
            write_json(workspace / f"chatSessions/{sid}.json", payload)

            result = run_cli(home, "repair-copilot-imports", "--apply")

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((workspace / f"chatSessions/{sid}.jsonl").exists())
            index = read_copilot_index(workspace)
            self.assertIn(sid, index["entries"])
            self.assertEqual(index["entries"][sid]["title"], "[Imported from Codex] Old import")
            agent_cache = read_vscode_json_key(workspace, COPILOT_AGENT_CACHE_KEY)
            self.assertTrue(any(item.get("resource") == vscode_local_session_uri(sid) for item in agent_cache))
            repaired_payload = json.loads((workspace / f"chatSessions/{sid}.json").read_text(encoding="utf-8"))
            self.assertEqual(len(repaired_payload["requests"]), 1)
            self.assertEqual(repaired_payload["requests"][0]["message"]["parts"][0]["kind"], "text")
            self.assertIn("editorRange", repaired_payload["requests"][0]["message"]["parts"][0])
            self.assertNotIn("<permissions instructions>", json.dumps(repaired_payload, ensure_ascii=False))

    def test_projection_skips_internal_system_messages(self) -> None:
        from chatbridge.models import Message, Session
        from chatbridge.writers import _project_session_messages

        session = Session(
            source="codex",
            session_id="s1",
            messages=[
                Message(role="developer", text="<permissions instructions>internal"),
                Message(role="system", text="<environment_context>internal"),
                Message(role="user", text="Visible prompt"),
                Message(role="assistant", text="Visible answer"),
            ],
        )

        projected = _project_session_messages(session)

        self.assertEqual([message.text for message in projected], ["Visible prompt", "Visible answer"])

    def test_text_extraction_replaces_embedded_image_data_urls(self) -> None:
        from chatbridge.util import text_from_any

        text = text_from_any(
            [
                {"type": "input_text", "text": "Please inspect this"},
                {"type": "input_image", "image_url": "data:image/png;base64,AAAA"},
            ]
        )

        self.assertIn("Please inspect this", text)
        self.assertIn("Image attachment not imported", text)
        self.assertIn("embedded PNG data URL", text)
        self.assertNotIn("input_image", text)
        self.assertNotIn("data:image", text)

    def test_projection_sanitizes_embedded_image_data_urls(self) -> None:
        from chatbridge.models import Message, Session
        from chatbridge.writers import _project_session_messages

        session = Session(
            source="codex",
            session_id="s1",
            messages=[Message(role="user", text="Logo screenshot\ninput_image data:image/png;base64,AAAA")],
        )

        projected = _project_session_messages(session)

        self.assertEqual(len(projected), 1)
        self.assertIn("Image attachment not imported", projected[0].text)
        self.assertNotIn("data:image", projected[0].text)

    def test_copilot_payload_normalization_sanitizes_embedded_image_data_urls(self) -> None:
        from chatbridge.writers import _copilot_payload_needs_cleanup, _normalize_copilot_payload

        payload = {
            "sessionId": "bad-image",
            "customTitle": "[Imported from Codex] Image",
            "isImported": True,
            "requests": [
                {
                    "message": {"text": "input_image data:image/png;base64,AAAA"},
                    "response": [{"value": "looks bad"}],
                }
            ],
        }

        self.assertTrue(_copilot_payload_needs_cleanup(payload))
        normalized = _normalize_copilot_payload(payload)
        request_text = json.dumps(normalized["requests"], ensure_ascii=False)
        self.assertIn("Image attachment not imported", request_text)
        self.assertNotIn("data:image", request_text)

    def test_copilot_running_guard_only_blocks_live_storage(self) -> None:
        from unittest.mock import patch

        from chatbridge import writers

        with tempfile.TemporaryDirectory() as temp_dir:
            home = Path(temp_dir) / "home"
            storage = home / ".config/Code/User/workspaceStorage"
            with patch.object(writers, "_is_vscode_running", return_value=True), patch.object(writers, "_real_user_home", return_value=home):
                with self.assertRaises(SystemExit):
                    writers._guard_copilot_write_when_vscode_running(home, storage, force=False)
                writers._guard_copilot_write_when_vscode_running(home, storage, force=True)

            other_home = Path(temp_dir) / "other-home"
            with patch.object(writers, "_is_vscode_running", return_value=True), patch.object(writers, "_real_user_home", return_value=other_home):
                writers._guard_copilot_write_when_vscode_running(home, storage, force=False)

    def test_codex_and_claude_limited_metadata_do_not_read_full_bodies(self) -> None:
        from unittest.mock import patch

        import chatbridge.parsers as parsers

        with tempfile.TemporaryDirectory() as temp_dir:
            home = make_home(Path(temp_dir))
            real_iter_jsonl = parsers.iter_jsonl

            def guarded_iter_jsonl(path: Path):
                text = str(path)
                if "/.codex/sessions/" in text or "/.claude/projects/" in text:
                    raise AssertionError(f"full body should not be read for limited metadata: {path}")
                return real_iter_jsonl(path)

            with patch("chatbridge.parsers.iter_jsonl", side_effect=guarded_iter_jsonl):
                codex_sessions = parsers.load_codex_sessions(home, metadata_only=True, limit=1)
                claude_sessions = parsers.load_claude_sessions(home, metadata_only=True, limit=1)

            self.assertEqual(len(codex_sessions), 1)
            self.assertEqual(len(claude_sessions), 1)



    def test_repair_codex_imports_normalizes_old_imported_titles_and_rollouts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            home = make_home(Path(temp_dir))
            root = home / ".codex"
            sid = "old-import"
            title = "[Imported from Copilot] Old Import"
            handoff = "[Handoff: Copilot -> Codex]\n\n## Source\n- Tool: Copilot\n"
            rollout = root / "sessions/imported/rollout-old-import.jsonl"
            write_jsonl(
                rollout,
                [
                    {"type": "session_meta", "payload": {"id": sid, "cwd": "/repo/app", "source": "chatbridge", "model_provider": "chatbridge"}},
                    {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": handoff}]}},
                    {"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Imported handoff context."}]}},
                    {"type": "event_msg", "payload": {"type": "user_message", "message": handoff, "images": []}},
                    {"type": "turn_context", "payload": {"cwd": "/repo/app", "model": "chatbridge-import"}},
                ],
            )
            append = {"id": sid, "thread_name": title, "updated_at": "2026-01-01T00:00:00Z"}
            with (root / "session_index.jsonl").open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(append, ensure_ascii=False) + "\n")
            con = sqlite3.connect(root / "state_5.sqlite")
            con.execute(
                """
                INSERT INTO threads (
                    id, rollout_path, created_at, updated_at, source, model_provider, cwd, title,
                    sandbox_policy, approval_mode, tokens_used, has_user_event, archived, cli_version,
                    first_user_message, memory_mode, model, reasoning_effort, created_at_ms, updated_at_ms,
                    thread_source, preview
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    sid,
                    str(rollout),
                    1700000200,
                    1700000200,
                    "chatbridge",
                    "chatbridge",
                    "/repo/app",
                    handoff,
                    "workspace-write",
                    "on-request",
                    0,
                    1,
                    0,
                    "chatbridge-0.1.0",
                    handoff,
                    "enabled",
                    "chatbridge-import",
                    "medium",
                    1700000200000,
                    1700000200000,
                    "local",
                    handoff,
                ),
            )
            con.commit()
            con.close()

            result = run_cli(home, "repair-codex-imports", "--apply")

            self.assertEqual(result.returncode, 0, result.stderr)
            con = sqlite3.connect(root / "state_5.sqlite")
            row = con.execute(
                """
                SELECT id, title, rollout_path, source, thread_source, model_provider, has_user_event, first_user_message
                FROM threads
                WHERE title = ?
                """,
                (title,),
            ).fetchone()
            con.close()
            self.assertNotEqual(row[0], sid)
            self.assertEqual(uuid.UUID(row[0]).version, 7)
            self.assertEqual(row[1], title)
            self.assertIn("/sessions/2023/", row[2])
            self.assertNotIn("-imported-", Path(row[2]).name)
            self.assertEqual(row[3], "cli")
            self.assertEqual(row[4], "user")
            self.assertEqual(row[5], "codex")
            self.assertEqual(row[6], 0)
            self.assertEqual(row[7], title)
            repaired_rows = [json.loads(line) for line in Path(row[2]).read_text(encoding="utf-8").splitlines()]
            self.assertEqual(repaired_rows[0]["payload"]["id"], row[0])
            self.assertEqual(repaired_rows[1]["payload"]["content"][0]["text"], title)
            self.assertIn("[Handoff: Copilot -> Codex]", repaired_rows[2]["payload"]["content"][0]["text"])

    def test_repair_codex_imports_appends_index_when_localizing_title_timezone(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            home = make_home(Path(temp_dir))
            root = home / ".codex"
            sid = "019e91f3-0689-7c14-a713-4869cab0913b"
            utc_title = "[Imported from Copilot] Fix auth · Copilot started 2023-11-14 22:13 UTC"
            local_title = "[Imported from Copilot] Fix auth · Copilot started 2023-11-15 06:13 HKT"
            rollout = root / "sessions/2026/01/01/rollout-2026-01-01T00-00-00-019e91f3-0689-7c14-a713-4869cab0913b.jsonl"
            write_jsonl(
                rollout,
                [
                    {"type": "session_meta", "payload": {"id": sid, "cwd": "/repo/app", "source": "cli", "thread_source": "user", "model_provider": "codex", "model": "gpt-5.5", "reasoning_effort": "medium"}},
                    {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "[Imported from Copilot] Fix auth"}]}},
                    {"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "[Handoff: Copilot -> Codex]\nImported by ChatNBridge. Treat this as context, not verified fact."}]}},
                    {"type": "event_msg", "payload": {"type": "user_message", "message": "[Imported from Copilot] Fix auth", "images": []}},
                    {"type": "turn_context", "payload": {"cwd": "/repo/app", "model": "gpt-5.5", "effort": "medium", "summary": local_title}},
                ],
            )
            with (root / "session_index.jsonl").open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({"id": sid, "thread_name": utc_title, "updated_at": "2026-01-01T00:00:00Z"}, ensure_ascii=False) + "\n")
            con = sqlite3.connect(root / "state_5.sqlite")
            con.execute(
                """
                INSERT INTO threads (
                    id, rollout_path, created_at, updated_at, source, model_provider, cwd, title,
                    sandbox_policy, approval_mode, tokens_used, has_user_event, archived, cli_version,
                    first_user_message, memory_mode, model, reasoning_effort, created_at_ms, updated_at_ms,
                    thread_source, preview
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    sid,
                    str(rollout),
                    1700000000,
                    1700000000,
                    "cli",
                    "codex",
                    "/repo/app",
                    local_title,
                    "workspace-write",
                    "on-request",
                    0,
                    0,
                    0,
                    "0.137.0",
                    "[Imported from Copilot] Fix auth",
                    "enabled",
                    "gpt-5.5",
                    "medium",
                    1700000000000,
                    1700000000000,
                    "user",
                    local_title,
                ),
            )
            con.commit()
            con.close()

            result = run_cli(home, "repair-codex-imports", "--apply", extra_env={"TZ": "Asia/Hong_Kong"})
            second = run_cli(home, "repair-codex-imports", "--dry-run", extra_env={"TZ": "Asia/Hong_Kong"})

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertIn("No Codex imports need repair", second.stdout)
            index_tail = (root / "session_index.jsonl").read_text(encoding="utf-8").splitlines()[-1]
            self.assertIn("Copilot started 2023-11-15 06:13", index_tail)

    def test_repair_codex_imports_preserves_visible_last_reply_when_cleaning_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            home = make_home(Path(temp_dir))
            root = home / ".codex"
            sid = "019e91f3-0689-7c14-a713-4869cab0913b"
            title = "[Imported from Copilot] Fix auth"
            legacy_marker = "Imported by ChatNBridge. Treat this as context, not verified fact."
            marker = "Imported by ChatBridge. Treat this as context, not verified fact."
            rollout = root / "sessions/2026/01/01/rollout-2026-01-01T00-00-00-019e91f3-0689-7c14-a713-4869cab0913b.jsonl"
            write_jsonl(
                rollout,
                [
                    {"type": "session_meta", "payload": {"id": sid, "cwd": "/repo/app", "source": "cli", "thread_source": "user", "model_provider": "codex", "model": "gpt-5.5", "reasoning_effort": "medium"}},
                    {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "[Copilot last query]\nFix login"}]}},
                    {"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": f"[Copilot last reply]\nUpdate auth handler.\n{marker}"}]}},
                    {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": title}]}},
                    {"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": f"[Handoff: Copilot -> Codex]\nFix login\n{legacy_marker}\nNested.\n{legacy_marker}"}]}},
                    {"type": "turn_context", "payload": {"cwd": "/repo/app", "model": "gpt-5.5", "effort": "medium", "summary": title}},
                ],
            )
            with (root / "session_index.jsonl").open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({"id": sid, "thread_name": title, "updated_at": "2026-01-01T00:00:00Z"}, ensure_ascii=False) + "\n")
            con = sqlite3.connect(root / "state_5.sqlite")
            con.execute(
                """
                INSERT INTO threads (
                    id, rollout_path, created_at, updated_at, source, model_provider, cwd, title,
                    sandbox_policy, approval_mode, tokens_used, has_user_event, archived, cli_version,
                    first_user_message, memory_mode, model, reasoning_effort, created_at_ms, updated_at_ms,
                    thread_source, preview
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    sid,
                    str(rollout),
                    1700000000,
                    1700000000,
                    "cli",
                    "codex",
                    "/repo/app",
                    title,
                    "workspace-write",
                    "on-request",
                    0,
                    0,
                    0,
                    "0.137.0",
                    title,
                    "enabled",
                    "gpt-5.5",
                    "medium",
                    1700000000000,
                    1700000000000,
                    "user",
                    title,
                ),
            )
            con.commit()
            con.close()

            result = run_cli(home, "repair-codex-imports", "--apply")

            self.assertEqual(result.returncode, 0, result.stderr)
            text = rollout.read_text(encoding="utf-8")
            self.assertEqual(text.count(marker), 1)
            self.assertNotIn(legacy_marker, text)
            rows = [json.loads(line) for line in text.splitlines()]
            assistant_texts = [
                row["payload"]["content"][0]["text"]
                for row in rows
                if row.get("type") == "response_item"
                and row.get("payload", {}).get("type") == "message"
                and row.get("payload", {}).get("role") == "assistant"
            ]
            self.assertTrue(assistant_texts[0].startswith("[Copilot last reply]"))
            self.assertTrue(assistant_texts[-1].startswith("[Handoff: Copilot -> Codex]"))



    def test_package_json_declares_npm_bin(self) -> None:
        package_path = ROOT / "package.json"

        package = json.loads(package_path.read_text(encoding="utf-8"))

        self.assertEqual(package["name"], "chatbridge")
        self.assertEqual(package["bin"]["chatbridge"], "bin/chatbridge")
        self.assertNotIn("chatnbridge", package["bin"])
        self.assertIn("rust/chatbridge-tui/Cargo.toml", package["files"])
        self.assertIn("rust/chatbridge-tui/Cargo.lock", package["files"])
        self.assertIn("rust/chatbridge-tui/src/**", package["files"])
        self.assertNotIn("rust/chatbridge-tui/target/**", package["files"])
        self.assertIn("test", package["scripts"])
        self.assertIn("test:rust", package["scripts"])

    def test_bin_chatbridge_is_node_launcher(self) -> None:
        launcher = (ROOT / "bin" / "chatbridge").read_text(encoding="utf-8")

        self.assertTrue(launcher.startswith("#!/usr/bin/env node"))
        self.assertIn("child_process", launcher)
        self.assertIn("PYTHONPATH", launcher)
        self.assertIn("runpy.run_module", launcher)
        self.assertNotIn("cwd: root", launcher)
        self.assertNotIn("#!/usr/bin/env bash", launcher)

    def test_release_installers_bootstrap_installed_package_ahead_of_cwd(self) -> None:
        shell_installer = (ROOT / "install.sh").read_text(encoding="utf-8")
        powershell_installer = (ROOT / "install.ps1").read_text(encoding="utf-8")

        self.assertIn("sys.path.insert(0, sys.argv.pop(1))", shell_installer)
        self.assertIn("runpy.run_module", shell_installer)
        self.assertIn("--uninstall", shell_installer)
        self.assertIn("CHATBRIDGE_PREFIX", shell_installer)
        self.assertIn("CHATBRIDGE_INSTALL_DIR", shell_installer)
        self.assertIn("CHATBRIDGE_INSTALLER_URL", shell_installer)
        self.assertNotIn("-m chatbridge \"\\$@\"", shell_installer)
        self.assertIn("sys.path.insert(0, sys.argv.pop(1))", powershell_installer)
        self.assertIn("runpy.run_module", powershell_installer)
        self.assertIn("[switch]$Uninstall", powershell_installer)
        self.assertIn("CHATBRIDGE_PREFIX", powershell_installer)
        self.assertIn("CHATBRIDGE_INSTALL_DIR", powershell_installer)
        self.assertIn("CHATBRIDGE_INSTALLER_URL", powershell_installer)
        self.assertNotIn("-m chatbridge @args", powershell_installer)

    def test_version_flag_reports_current_version(self) -> None:
        from chatbridge import __version__

        with tempfile.TemporaryDirectory() as temp_dir:
            home = make_home(Path(temp_dir))

            result = run_cli(home, "--version")

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(f"chatbridge {__version__}", result.stdout)

    def test_update_requires_release_installer_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            home = make_home(Path(temp_dir))

            result = run_cli(home, "update")

            self.assertEqual(result.returncode, 2)
            self.assertIn("release-installer installs", result.stderr)

    def test_update_runs_installer_with_recorded_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            home = make_home(temp)
            prefix = temp / "prefix"
            install_dir = temp / "install"
            marker = temp / "update-marker.txt"
            installer = temp / "install.sh"
            installer.write_text(
                "\n".join(
                    [
                        "#!/usr/bin/env bash",
                        "set -euo pipefail",
                        "printf 'prefix=%s\\ninstall_dir=%s\\nargs=%s\\n' \"$CHATBRIDGE_PREFIX\" \"$CHATBRIDGE_INSTALL_DIR\" \"$*\" > \"$CHATBRIDGE_UPDATE_MARKER\"",
                        "",
                    ]
                ),
                encoding="utf-8",
            )

            result = run_cli(
                home,
                "update",
                extra_env={
                    "CHATBRIDGE_PREFIX": str(prefix),
                    "CHATBRIDGE_INSTALL_DIR": str(install_dir),
                    "CHATBRIDGE_INSTALLER_URL": installer.as_uri(),
                    "CHATBRIDGE_UPDATE_MARKER": str(marker),
                },
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("chatbridge update: installing latest", result.stdout)
            marker_text = marker.read_text(encoding="utf-8")
            self.assertIn(f"prefix={prefix}", marker_text)
            self.assertIn(f"install_dir={install_dir}", marker_text)
            self.assertIn(f"--prefix {prefix} --dir {install_dir} --version latest", marker_text)

    def test_no_args_opens_rust_tui_smoke(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            home = make_home(temp)
            rust_tui = make_fake_rust_tui(temp / "chatbridge-tui")
            env = os.environ.copy()
            env["HOME"] = str(home)
            env["PYTHONPATH"] = str(ROOT)
            env["CHATBRIDGE_TUI_SMOKE"] = "1"
            env["CHATBRIDGE_TUI_BIN"] = str(rust_tui)

            result = subprocess.run(
                [sys.executable, "-m", "chatbridge"],
                cwd=ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("ChatBridge TUI", result.stdout)
            self.assertIn("Rust ratatui TUI", result.stdout)
            self.assertIn("GitHub Copilot", result.stdout)
            self.assertIn("Codex CLI", result.stdout)
            self.assertIn("Prompt Handoff", result.stdout)
            self.assertIn("Native Import", result.stdout)
            self.assertNotIn("Command >", result.stdout)
            self.assertIn("↑/↓", result.stdout)
            self.assertNotIn("Choose source", result.stdout)

    def test_no_args_requires_rust_tui_binary(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            home = make_home(Path(temp_dir))
            env = os.environ.copy()
            env["HOME"] = str(home)
            env["PYTHONPATH"] = str(ROOT)
            env["CHATBRIDGE_TUI_BIN"] = str(Path(temp_dir) / "missing-chatbridge-tui")

            result = subprocess.run(
                [sys.executable, "-m", "chatbridge"],
                cwd=ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 1)
            self.assertIn("CHATBRIDGE_TUI_BIN is set to", result.stderr)
            self.assertIn("missing-chatbridge-tui", result.stderr)
            self.assertIn("Unset CHATBRIDGE_TUI_BIN", result.stderr)

    def test_no_args_reports_wrong_platform_rust_tui_binary(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            home = make_home(temp)
            bad_binary = temp / "chatbridge-tui"
            bad_binary.write_text("not a native executable", encoding="utf-8")
            bad_binary.chmod(0o755)
            env = os.environ.copy()
            env["HOME"] = str(home)
            env["PYTHONPATH"] = str(ROOT)
            env["CHATBRIDGE_TUI_SMOKE"] = "1"
            env["CHATBRIDGE_TUI_BIN"] = str(bad_binary)

            result = subprocess.run(
                [sys.executable, "-m", "chatbridge"],
                cwd=ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 1)
            self.assertIn("Rust TUI binary is not runnable", result.stderr)
            self.assertIn(str(bad_binary), result.stderr)
            self.assertNotIn("Traceback", result.stderr)

    def test_npm_bin_wrapper_starts_tui_smoke(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            home = make_home(temp)
            rust_tui = make_fake_rust_tui(temp / "chatbridge-tui")
            env = os.environ.copy()
            env["HOME"] = str(home)
            env["PYTHON"] = sys.executable
            env["CHATBRIDGE_TUI_SMOKE"] = "1"
            env["CHATBRIDGE_TUI_BIN"] = str(rust_tui)

            result = subprocess.run(
                [str(ROOT / "bin" / "chatbridge")],
                cwd=ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("ChatBridge TUI", result.stdout)

    def test_npm_bin_wrapper_ignores_local_shadow_package(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            home = make_home(temp)
            rust_tui = make_fake_rust_tui(temp / "chatbridge-tui")
            shadow_package = temp / "chatbridge"
            shadow_package.mkdir()
            (shadow_package / "__init__.py").write_text("", encoding="utf-8")
            (shadow_package / "__main__.py").write_text("raise SystemExit('shadow package used')\n", encoding="utf-8")
            env = os.environ.copy()
            env["HOME"] = str(home)
            env["PYTHON"] = sys.executable
            env["CHATBRIDGE_TUI_SMOKE"] = "1"
            env["CHATBRIDGE_TUI_BIN"] = str(rust_tui)

            result = subprocess.run(
                [str(ROOT / "bin" / "chatbridge")],
                cwd=temp,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("ChatBridge TUI", result.stdout)
            self.assertNotIn("shadow package used", result.stderr)

    def test_npm_bin_wrapper_resolves_global_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            home = make_home(temp)
            rust_tui = make_fake_rust_tui(temp / "chatbridge-tui")
            link = temp / "prefix" / "bin" / "chatbridge"
            link.parent.mkdir(parents=True)
            link.symlink_to(ROOT / "bin" / "chatbridge")
            env = os.environ.copy()
            env["HOME"] = str(home)
            env["PYTHON"] = sys.executable
            env["CHATBRIDGE_TUI_SMOKE"] = "1"
            env["CHATBRIDGE_TUI_BIN"] = str(rust_tui)

            result = subprocess.run(
                [str(link)],
                cwd=temp,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("ChatBridge TUI", result.stdout)

    def test_homebrew_formula_template_exists(self) -> None:
        formula = (ROOT / "packaging" / "homebrew" / "chatbridge.rb").read_text(encoding="utf-8")

        self.assertIn("class Chatbridge < Formula", formula)
        self.assertIn('depends_on "rust" => :build', formula)
        self.assertIn("cargo", formula)
        self.assertIn("chatbridge-tui", formula)
        self.assertIn("bin.install", formula)
        self.assertIn("chatbridge", formula)
        self.assertIn("runpy.run_module", formula)

    def test_path_doctor_reports_cross_platform_candidates_and_configured_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            home = make_home(Path(temp_dir))
            override = home / "custom-copilot"
            override.mkdir()
            config_dir = home / ".chatbridge"
            config_dir.mkdir(exist_ok=True)
            (config_dir / "config.json").write_text(
                json.dumps({"paths": {"copilot_workspace_storage": str(override)}}),
                encoding="utf-8",
            )

            result = run_cli(home, "paths", "doctor")

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("ChatBridge Path Doctor", result.stdout)
            self.assertIn("copilot_workspace_storage", result.stdout)
            self.assertIn(str(override), result.stdout)
            self.assertIn("Library/Application Support/Code/User/workspaceStorage", result.stdout)
            self.assertIn("AppData/Roaming/Code/User/workspaceStorage", result.stdout)
            self.assertIn("Config file:", result.stdout)
            self.assertIn("chatbridge paths edit", result.stdout)
            self.assertIn("chatbridge paths set", result.stdout)

    def test_path_set_writes_config_override(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            home = make_home(Path(temp_dir))
            custom = home / "custom-codex"

            result = run_cli(home, "paths", "set", "--codex-home", str(custom))

            self.assertEqual(result.returncode, 0, result.stderr)
            config = json.loads((home / ".chatbridge/config.json").read_text(encoding="utf-8"))
            self.assertEqual(config["paths"]["codex_home"], str(custom))

    def test_api_path_set_writes_config_override(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            home = make_home(Path(temp_dir))
            custom = home / "custom-claude"

            result = run_cli(home, "api", "paths", "set", "--claude-home", str(custom))

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertTrue(payload["ok"])
            self.assertIn("Wrote ChatBridge path config", payload["data"]["text"])
            config = json.loads((home / ".chatbridge/config.json").read_text(encoding="utf-8"))
            self.assertEqual(config["paths"]["claude_home"], str(custom))

    def test_path_edit_creates_config_template_and_uses_editor(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            home = make_home(Path(temp_dir))

            result = run_cli(home, "paths", "edit", "--editor", "true")

            self.assertEqual(result.returncode, 0, result.stderr)
            config = json.loads((home / ".chatbridge/config.json").read_text(encoding="utf-8"))
            self.assertIn("copilot_workspace_storage", config["paths"])
            self.assertIn("codex_home", config["paths"])
            self.assertIn("claude_home", config["paths"])


if __name__ == "__main__":
    unittest.main()
