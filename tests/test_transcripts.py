"""Tool-aware transcripts: Codex/Claude parsing, titles, projection budget, bundles."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from test_chatbridge import make_home, run_cli, write_jsonl

from chatbridge.export import build_bundle, list_bundles, load_bundle, read_bundle_header, write_bundle
from chatbridge.models import Message, Session
from chatbridge.parsers import claude_transcript_messages, codex_rollout_messages, load_claude_session, load_claude_sessions, load_codex_session
from chatbridge.summary import build_handoff, touched_files
from chatbridge.toolcalls import clip
from chatbridge.writers import project_transcript


def item_row(item: dict, ts: str = "2026-10-01T00:00:00Z") -> dict:
    return {"timestamp": ts, "type": "event_msg", "payload": {"type": "item_completed", "item": item}}


def response_row(payload: dict, ts: str = "2026-07-01T00:00:00Z") -> dict:
    return {"timestamp": ts, "type": "response_item", "payload": payload}


class CodexParsingTests(unittest.TestCase):
    def test_paginated_rollout_keeps_commands_diffs_and_order(self) -> None:
        rows = [
            {"type": "session_meta", "payload": {"id": "t1", "cwd": "/repo"}},
            response_row({"type": "message", "role": "developer", "content": [{"type": "input_text", "text": "<skills_instructions>x"}]}),
            response_row({"type": "message", "role": "user", "content": [{"type": "input_text", "text": "<environment_context>cwd</environment_context>"}]}),
            item_row({"type": "UserMessage", "content": [{"type": "text", "text": "Fix the failing test"}]}),
            item_row({"type": "Reasoning", "summary_text": [], "raw_content": []}),
            item_row({"type": "AgentMessage", "content": [{"type": "Text", "text": "Running the tests first."}], "phase": "commentary"}),
            item_row({
                "type": "CommandExecution",
                "command": ["/bin/bash", "-lc", "npm test"],
                "cwd": "file:///repo",
                "aggregated_output": "1 failing\nexpected 2 got 3",
                "exit_code": 1,
            }),
            item_row({
                "type": "FileChange",
                "changes": {
                    "/repo/src/add.js": {"type": "update", "unified_diff": "@@ -1 +1 @@\n-return a + b + 1\n+return a + b"},
                    "/repo/NOTES.md": {"type": "add", "content": "fixed add\n"},
                },
                "status": "completed",
            }),
            item_row({"type": "WebSearch", "query": "jest expect toBe", "action": {"type": "search"}}),
            item_row({"type": "ContextCompaction"}),
            item_row({"type": "AgentMessage", "content": [{"type": "Text", "text": "Fixed: off-by-one in add()."}], "phase": "final_answer"}),
            # paginated rollouts also carry the raw model items; they must not duplicate the above
            response_row({"type": "custom_tool_call", "name": "exec", "call_id": "c1", "input": "tools.exec_command({cmd:'npm test'})"}),
        ]

        messages = codex_rollout_messages(rows)

        self.assertEqual([m.role for m in messages], ["user", "assistant", "tool", "tool", "tool", "assistant"])
        self.assertEqual(messages[0].text, "Fix the failing test")
        shell = messages[2]
        self.assertEqual(shell.tool, "shell")
        self.assertIn("$ npm test", shell.text)
        self.assertIn("(cwd: /repo)", shell.text)
        self.assertIn("expected 2 got 3", shell.text)
        self.assertIn("(exit code 1)", shell.text)
        patch = messages[3]
        self.assertEqual(patch.tool, "apply_patch")
        self.assertIn("Updated /repo/src/add.js", patch.text)
        self.assertIn("+return a + b", patch.text)
        self.assertIn("Added /repo/NOTES.md", patch.text)
        self.assertIn("+fixed add", patch.text)
        self.assertIn("jest expect toBe", messages[4].text)
        self.assertNotIn("tools.exec_command", "\n".join(m.text for m in messages))

    def test_legacy_rollout_pairs_function_calls_with_outputs(self) -> None:
        rows = [
            response_row({"type": "message", "role": "user", "content": [{"type": "input_text", "text": "# Context from my IDE setup:\n\n## Active file: a.py\n\n## My request for Codex:\nwhy is CI red?\n"}]}),
            {"type": "event_msg", "payload": {"type": "user_message", "message": "why is CI red?"}},
            response_row({"type": "reasoning", "summary": [], "encrypted_content": "gAAAA"}),
            response_row({"type": "function_call", "name": "exec_command", "call_id": "c1", "arguments": json.dumps({"cmd": "pytest -q", "workdir": "/repo"})}),
            response_row({"type": "function_call_output", "call_id": "c1", "output": "Chunk ID: ab12\nWall time: 0.5 seconds\nProcess running with session ID 7\nOriginal token count: 9\nOutput:\nrunning..."}),
            response_row({"type": "function_call", "name": "write_stdin", "call_id": "c2", "arguments": json.dumps({"session_id": 7, "chars": ""})}),
            response_row({"type": "function_call_output", "call_id": "c2", "output": "Chunk ID: cd34\nWall time: 3 seconds\nProcess exited with code 1\nOriginal token count: 9\nOutput:\nFAILED test_x.py::test_add"}),
            response_row({"type": "custom_tool_call", "name": "apply_patch", "call_id": "c3", "input": "*** Begin Patch\n*** Update File: /repo/x.py\n@@\n-a\n+b\n*** End Patch"}),
            response_row({"type": "custom_tool_call_output", "call_id": "c3", "output": json.dumps({"output": "Success. Updated the following files:\nM /repo/x.py\n", "metadata": {"exit_code": 0}})}),
            response_row({"type": "function_call", "name": "update_plan", "call_id": "c4", "arguments": json.dumps({"plan": [{"step": "fix", "status": "completed"}, {"step": "ship", "status": "pending"}]})}),
            response_row({"type": "web_search_call", "status": "completed", "action": {"type": "search", "query": "pytest flaky"}}),
            response_row({"type": "message", "role": "user", "content": [{"type": "input_text", "text": "<user_shell_command>\n<command>\nls\n</command>\n<result>\nExit code: 0\nDuration: 0.01 seconds\nOutput:\nREADME.md\n</result>\n</user_shell_command>"}]}),
            response_row({"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "CI fixed."}]}),
            {"type": "event_msg", "payload": {"type": "agent_message", "message": "CI fixed."}},
        ]

        messages = codex_rollout_messages(rows)

        self.assertEqual(messages[0].role, "user")
        self.assertEqual(messages[0].text, "why is CI red?")
        shell = messages[1]
        self.assertIn("$ pytest -q", shell.text)
        self.assertIn("running...", shell.text)
        self.assertIn("FAILED test_x.py::test_add", shell.text)  # write_stdin polling output joins the command
        self.assertIn("(exit code 1)", shell.text)
        self.assertNotIn("Chunk ID", shell.text)
        self.assertIn("*** Update File: /repo/x.py", messages[2].text)
        self.assertIn("Success. Updated the following files", messages[2].text)
        self.assertIn("- [x] fix", messages[3].text)
        self.assertIn("- [ ] ship", messages[3].text)
        self.assertIn("pytest flaky", messages[4].text)
        self.assertEqual(messages[5].tool, "user_shell")
        self.assertIn("README.md", messages[5].text)
        self.assertEqual([m.role for m in messages].count("assistant"), 1)

    def test_codex_title_prefers_codex_thread_name(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            home = make_home(Path(temp_dir))
            con = sqlite3.connect(home / ".codex/state_5.sqlite")
            con.execute("ALTER TABLE threads ADD COLUMN name TEXT")
            con.execute("UPDATE threads SET name = 'Named by Codex', title = 'hello' WHERE id = 'native-codex-1'")
            con.commit()
            con.close()

            result = run_cli(home, "api", "sessions", "--source", "codex", "--limit", "5")
            session = load_codex_session(home, "native-codex-1")

            titles = {row["sessionId"]: row["title"] for row in json.loads(result.stdout)["data"]["sessions"]}
            self.assertEqual(titles["native-codex-1"], "Named by Codex")
            self.assertEqual(session.title, "Named by Codex")


class ClaudeParsingTests(unittest.TestCase):
    def rows(self) -> list[dict]:
        return [
            {"type": "user", "isMeta": True, "message": {"role": "user", "content": "<local-command-caveat>Caveat</local-command-caveat>"}},
            {"type": "user", "message": {"role": "user", "content": "<command-name>/effort</command-name>"}},
            {"type": "user", "message": {"role": "user", "content": "Refactor the parser"}, "cwd": "/repo", "timestamp": "t1"},
            {"type": "assistant", "message": {"id": "m1", "role": "assistant", "content": [{"type": "thinking", "thinking": "", "signature": "EpVEC" * 50}]}},
            {"type": "assistant", "message": {"id": "m1", "role": "assistant", "content": [{"type": "text", "text": "Looking at it."}]}},
            {"type": "assistant", "message": {"id": "m1", "role": "assistant", "content": [{"type": "text", "text": "Plan: split it."}]}},
            {"type": "assistant", "message": {"id": "m1", "role": "assistant", "content": [{"type": "tool_use", "id": "tu1", "name": "Edit", "input": {"file_path": "/repo/p.py", "old_string": "a = 1", "new_string": "a = 2"}}]}},
            {"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "tu1", "content": "The file /repo/p.py has been updated."}]}},
            {"type": "assistant", "message": {"id": "m1", "role": "assistant", "content": [{"type": "text", "text": "Edited."}]}},
            {"type": "assistant", "message": {"id": "m2", "role": "assistant", "content": [{"type": "tool_use", "id": "tu2", "name": "Bash", "input": {"command": "pytest", "description": "Run tests"}}]}},
            {"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "tu2", "content": [{"type": "text", "text": "3 passed"}], "is_error": False}]}},
            {"type": "user", "isSidechain": True, "message": {"role": "user", "content": "subagent prompt"}},
            {"type": "user", "message": {"role": "user", "content": "<task-notification>done</task-notification>"}},
            {"type": "user", "message": {"role": "user", "content": "<bash-input>git status</bash-input>"}},
            {"type": "user", "message": {"role": "user", "content": "<bash-stdout>clean</bash-stdout><bash-stderr></bash-stderr>"}},
            {"type": "assistant", "isApiErrorMessage": True, "message": {"role": "assistant", "content": [{"type": "text", "text": "API Error: 529"}]}},
            {"type": "user", "message": {"role": "user", "content": "继续"}},
            {"type": "user", "message": {"role": "user", "content": "继续"}},
            {"type": "ai-title", "aiTitle": "Refactor parser"},
            {"type": "custom-title", "customTitle": "parser-refactor"},
        ]

    def test_claude_blocks_become_ordered_messages_and_tools(self) -> None:
        messages, titles, cwd = claude_transcript_messages(self.rows())

        self.assertEqual(cwd, "/repo")
        self.assertEqual(titles, {"ai": "Refactor parser", "custom": "parser-refactor"})
        self.assertEqual(
            [(m.role, m.tool) for m in messages],
            [("user", None), ("assistant", None), ("tool", "Edit"), ("assistant", None), ("tool", "Bash"), ("tool", "user_shell"), ("user", None)],
        )
        self.assertEqual(messages[1].text, "Looking at it.\n\nPlan: split it.")  # adjacent text rows of one message merge
        self.assertIn("-a = 1", messages[2].text)
        self.assertIn("+a = 2", messages[2].text)
        self.assertIn("has been updated", messages[2].text)
        self.assertEqual(messages[3].text, "Edited.")  # not merged across the tool call
        self.assertIn("$ pytest", messages[4].text)
        self.assertIn("3 passed", messages[4].text)
        self.assertIn("clean", messages[5].text)
        all_text = "\n".join(m.text for m in messages)
        for noise in ("EpVEC", "Caveat", "/effort", "subagent prompt", "task-notification", "API Error"):
            self.assertNotIn(noise, all_text)
        # adjacent identical prompts collapse to one turn
        self.assertEqual(messages[-1].text, "继续")

    def test_claude_titles_skip_commands_and_hide_resume_only_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            home = make_home(Path(temp_dir))
            claude = home / ".claude"
            write_jsonl(
                claude / "history.jsonl",
                [
                    {"display": "/effort high", "timestamp": 1700000040000, "project": "/repo/app", "sessionId": "claude-2"},
                    {"display": "Add caching to the API", "timestamp": 1700000041000, "project": "/repo/app", "sessionId": "claude-2"},
                    {"display": "/exit", "timestamp": 1700000042000, "project": "/repo/app", "sessionId": "claude-2"},
                    {"display": "/resume", "timestamp": 1700000043000, "project": "/repo/app", "sessionId": "resume-only"},
                ],
            )
            write_jsonl(claude / "projects/-repo-app/claude-2.jsonl", [{"type": "user", "message": {"content": "Add caching to the API"}, "cwd": "/repo/app"}])

            listed = {s.session_id: s.title for s in load_claude_sessions(home, metadata_only=True, limit=10)}
            full = load_claude_session(home, "claude-2")

            self.assertEqual(listed["claude-2"], "Add caching to the API")
            self.assertNotIn("resume-only", listed)
            self.assertEqual(full.title, "Add caching to the API")

            write_jsonl(
                claude / "projects/-repo-app/claude-2.jsonl",
                [
                    {"type": "user", "message": {"content": "Add caching to the API"}, "cwd": "/repo/app"},
                    {"type": "ai-title", "aiTitle": "API response caching"},
                ],
            )
            listed = {s.session_id: s.title for s in load_claude_sessions(home, metadata_only=True, limit=10)}
            self.assertEqual(listed["claude-2"], "API response caching")


class CodexConfigTests(unittest.TestCase):
    def test_simple_toml_reader_handles_quotes_comments_and_profiles(self) -> None:
        from chatbridge.codex_config import parse_simple_toml, read_codex_settings

        text = (
            '# comment\nmodel = "gpt-6"  # trailing\nmodel_provider = \'acme\'\nprofile = "work"\n'
            'approvals = ["a", "b"]\n\n[model_providers.acme]\nname = "Acme gateway"\n\n'
            '[profiles.work]\nmodel = "o5"\nmodel_reasoning_effort = "high"\n\n[profiles."other one"]\nmodel = "x"\n'
        )
        parsed = parse_simple_toml(text)
        self.assertEqual(parsed["model"], "gpt-6")
        self.assertEqual(parsed["model_provider"], "acme")
        self.assertEqual(parsed["model_providers"]["acme"]["name"], "Acme gateway")
        self.assertEqual(parsed["profiles"]["other one"]["model"], "x")
        self.assertNotIn("approvals", parsed)

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "config.toml").write_text(text, encoding="utf-8")
            settings = read_codex_settings(root)
        self.assertEqual(settings, {"model_provider": "acme", "model": "o5", "model_reasoning_effort": "high"})

    def test_codex_import_uses_configured_provider_and_model(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            home = make_home(Path(temp_dir))
            (home / ".codex/config.toml").write_text('model = "acme-large"\nmodel_provider = "acme"\n\n[model_providers.acme]\nname = "x"\n', encoding="utf-8")

            result = run_cli(home, "native-import", "--from", "claude", "--to", "codex", "--session", "claude-1", "--apply")

            self.assertEqual(result.returncode, 0, result.stderr)
            con = sqlite3.connect(home / ".codex/state_5.sqlite")
            provider, model = con.execute("SELECT model_provider, model FROM threads WHERE title LIKE '[Imported from Claude Code]%'").fetchone()
            con.close()
            self.assertEqual((provider, model), ("acme", "acme-large"))
            session_file = next(Path(line.split("Session file: ", 1)[1]) for line in result.stdout.splitlines() if line.startswith("Session file: "))
            rows = [json.loads(line) for line in session_file.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(rows[0]["payload"]["model_provider"], "acme")
            self.assertEqual(rows[-1]["payload"]["model"], "acme-large")

    def test_repair_moves_old_codex_provider_imports_without_touching_turns(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            home = make_home(Path(temp_dir))
            imported = run_cli(home, "native-import", "--from", "claude", "--to", "codex", "--session", "claude-1", "--apply")
            self.assertEqual(imported.returncode, 0, imported.stderr)
            session_file = next(Path(line.split("Session file: ", 1)[1]) for line in imported.stdout.splitlines() if line.startswith("Session file: "))
            con = sqlite3.connect(home / ".codex/state_5.sqlite")
            sid, title = con.execute("SELECT id, title FROM threads WHERE title LIKE '[Imported from Claude Code]%'").fetchone()
            # Simulate an import written by ChatBridge <= 1.0.2 (hard-coded "codex" provider).
            con.execute("UPDATE threads SET model_provider = 'codex', model = 'gpt-5.5', reasoning_effort = 'medium' WHERE id = ?", (sid,))
            con.commit()
            con.close()
            before_users = self.user_texts(session_file)

            fresh = run_cli(home, "repair-codex-imports", "--dry-run")
            applied = run_cli(home, "repair-codex-imports", "--apply")
            again = run_cli(home, "repair-codex-imports", "--dry-run")

            self.assertIn("provider codex -> openai", fresh.stdout)
            self.assertEqual(applied.returncode, 0, applied.stderr)
            self.assertIn("No Codex imports need repair", again.stdout)
            con = sqlite3.connect(home / ".codex/state_5.sqlite")
            provider, model, new_title = con.execute("SELECT model_provider, model, title FROM threads WHERE id = ?", (sid,)).fetchone()
            con.close()
            self.assertEqual((provider, model, new_title), ("openai", None, title))
            self.assertEqual(self.user_texts(session_file), before_users)
            self.assertGreater(len(before_users), 1)

    def test_fresh_import_is_not_flagged_for_repair(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            home = make_home(Path(temp_dir))
            run_cli(home, "native-import", "--from", "claude", "--to", "codex", "--session", "claude-1", "--apply")

            result = run_cli(home, "repair-codex-imports", "--dry-run")

            self.assertIn("No Codex imports need repair", result.stdout)

    @staticmethod
    def user_texts(path: Path) -> list[str]:
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        return [
            row["payload"]["message"]
            for row in rows
            if row.get("type") == "event_msg" and row.get("payload", {}).get("type") == "user_message"
        ]


def tool_session(turns: int = 100, output_chars: int = 3000) -> Session:
    messages: list[Message] = []
    for turn in range(turns):
        messages.append(Message(role="user", text=f"step {turn}: 继续"))
        messages.append(Message(role="tool", tool="shell", text=f"[Tool: shell] $ make step{turn}\n--- output ---\n" + ("x" * output_chars)))
        messages.append(Message(role="assistant", text=f"done with step {turn}"))
    return Session(source="codex", session_id="s", title="long", project_path="/repo", messages=messages)


class ProjectionTests(unittest.TestCase):
    def test_tool_runs_become_fenced_assistant_blocks(self) -> None:
        session = Session(
            source="codex",
            session_id="s",
            messages=[
                Message(role="user", text="go"),
                Message(role="tool", tool="shell", text="[Tool: shell] $ echo ```hi```"),
                Message(role="tool", tool="shell", text="[Tool: shell] $ ls"),
                Message(role="assistant", text="done"),
                Message(role="user", text="继续"),
                Message(role="assistant", text="ok"),
                Message(role="user", text="继续"),
            ],
        )

        projected = project_transcript(session).messages

        self.assertEqual([m.role for m in projected], ["user", "assistant", "assistant", "user", "assistant", "user"])
        block = projected[1].text
        self.assertTrue(block.startswith("````text\n[Tool: shell] $ echo ```hi```\n````"))
        self.assertIn("```text\n[Tool: shell] $ ls\n```", block)
        self.assertEqual([m.text for m in projected if m.role == "user"].count("继续"), 2)

    def test_budget_trims_tools_before_conversation_deterministically(self) -> None:
        session = tool_session()

        first = project_transcript(session, 8_000)
        second = project_transcript(session, 8_000)

        self.assertEqual([m.text for m in first.messages], [m.text for m in second.messages])
        self.assertLessEqual(first.approx_tokens, 8_800)
        texts = "\n".join(m.text for m in first.messages)
        for turn in range(100):
            self.assertIn(f"step {turn}: 继续", texts)
            self.assertIn(f"done with step {turn}", texts)
        self.assertTrue(any("shortened" in note for note in first.notes))
        self.assertIn("left out", "; ".join(first.notes))
        self.assertIn("[ChatBridge:", texts)

    def test_tiny_budget_keeps_first_prompt_and_last_message(self) -> None:
        projection = project_transcript(tool_session(turns=30), 300)
        texts = [m.text for m in projection.messages]
        self.assertEqual(texts[0], "step 0: 继续")
        self.assertEqual(texts[-1], "done with step 29")

    def test_recovered_artifacts_are_trimmed_before_conversation(self) -> None:
        from chatbridge.models import Artifact

        session = tool_session(turns=40, output_chars=0)
        session.messages = [m for m in session.messages if m.role != "tool"]
        session.artifacts = [Artifact(kind="tool-result", text=f"result {index} " + "r" * 4000) for index in range(200)]

        projection = project_transcript(session, 6_000)

        texts = "\n".join(m.text for m in projection.messages)
        for turn in range(40):
            self.assertIn(f"step {turn}: 继续", texts)
        self.assertLessEqual(projection.approx_tokens, 6_600)
        self.assertIn("left out", "; ".join(projection.notes))

    def test_cli_reports_trimming_and_max_tokens_zero_disables_it(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            home = make_home(temp)
            bundle = temp / "big.json"
            bundle.write_text(json.dumps(build_bundle(tool_session())), encoding="utf-8")

            trimmed = run_cli(home, "native-import", "--bundle", str(bundle), "--to", "claude", "--dry-run")
            full = run_cli(home, "native-import", "--bundle", str(bundle), "--to", "claude", "--dry-run", "--max-tokens", "0")

            self.assertIn("Trimmed to fit:", trimmed.stdout)
            self.assertIn("--max-tokens 0", trimmed.stdout)
            self.assertIn("no token budget", full.stdout)
            self.assertNotIn("Trimmed to fit:", full.stdout)

    def test_claude_import_of_codex_tools_keeps_commands(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            home = make_home(temp)
            bundle = temp / "b.json"
            bundle.write_text(json.dumps(build_bundle(tool_session(turns=3, output_chars=50))), encoding="utf-8")

            result = run_cli(home, "native-import", "--bundle", str(bundle), "--to", "claude", "--apply")

            self.assertEqual(result.returncode, 0, result.stderr)
            session_file = next(Path(line.split("Session file: ", 1)[1]) for line in result.stdout.splitlines() if line.startswith("Session file: "))
            text = session_file.read_text(encoding="utf-8")
            self.assertIn("$ make step2", text)


class BundleTests(unittest.TestCase):
    def test_bundle_carries_tool_messages_and_stats_header(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            session = tool_session(turns=2, output_chars=10)
            path = write_bundle(session, str(temp / "exports") + "/")

            self.assertEqual(path.parent, temp / "exports")
            raw = path.read_text(encoding="utf-8")
            self.assertLess(raw.index('"stats"'), raw.index('"session"'))
            header = read_bundle_header(path)
            self.assertEqual(header["stats"]["toolCalls"], 2)
            self.assertEqual(header["source"], "codex")
            self.assertEqual(header["title"], "long")
            loaded = load_bundle(path)
            self.assertEqual([m.tool for m in loaded.messages if m.role == "tool"], ["shell", "shell"])

            (temp / "exports" / "chatbridge-export-codex-not-a-bundle.json").write_text("{}", encoding="utf-8")
            found = list_bundles([temp / "exports", temp])
            self.assertEqual([row["path"] for row in found], [str(path)])

    def test_api_preview_bundles_and_paths_status(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            home = make_home(temp)
            exported = run_cli(home, "api", "export", "--from", "claude", "--session", "claude-1", "--out", str(home / ".chatbridge/exports") + "/")
            self.assertTrue(json.loads(exported.stdout)["ok"], exported.stdout)

            bundles = json.loads(run_cli(home, "api", "bundles").stdout)["data"]
            self.assertEqual(len(bundles["bundles"]), 1)
            bundle_path = bundles["bundles"][0]["path"]
            self.assertEqual(bundles["exportDir"], str(home / ".chatbridge/exports"))

            preview = json.loads(run_cli(home, "api", "preview", "--bundle", bundle_path).stdout)["data"]
            self.assertEqual(preview["title"], "Explain repo")
            self.assertEqual(preview["stats"]["user"], 1)
            self.assertFalse(preview["projectExists"])  # /repo/app does not exist here
            self.assertIn("importEstimate", preview)

            status = json.loads(run_cli(home, "api", "paths", "status").stdout)["data"]["sources"]
            self.assertEqual({row["source"] for row in status}, {"copilot", "codex", "claude"})
            self.assertTrue(all(row["exists"] for row in status))

    def test_handoff_separates_tool_activity_and_lists_files(self) -> None:
        session = Session(
            source="codex",
            session_id="s",
            messages=[
                Message(role="user", text="fix it"),
                Message(role="tool", tool="apply_patch", text="[Tool: apply_patch] edit /repo/a.py\n-x\n+y"),
                Message(role="tool", tool="shell", text="[Tool: shell] $ pytest\n--- output ---\nok"),
                Message(role="assistant", text="fixed"),
            ],
        )

        handoff = build_handoff(session, "claude")

        self.assertIn("- user: fix it", handoff)
        self.assertIn("- assistant: fixed", handoff)
        self.assertIn("## Files touched\n- /repo/a.py", handoff)
        self.assertIn("shell: $ pytest", handoff)
        self.assertEqual(touched_files(session), ["/repo/a.py"])

    def test_clip_keeps_head_and_tail(self) -> None:
        text = "a" * 100 + "MIDDLE" + "z" * 100
        clipped = clip(text, 60)
        self.assertTrue(clipped.startswith("a" * 40))
        self.assertTrue(clipped.endswith("z" * 20))
        self.assertIn("chars omitted", clipped)


if __name__ == "__main__":
    unittest.main()
