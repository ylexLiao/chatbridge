#!/usr/bin/env python3
"""Check that ChatBridge's Codex imports work with real Codex binaries.

For each binary this creates a throwaway CODEX_HOME, lets that Codex
initialize it, imports a bundle with ChatBridge, then asks Codex itself (via
`codex app-server` JSON-RPC, no model calls) whether it:

  1. lists the import in its default thread list (current provider only),
  2. can resume it,
  3. shows the imported turns, including tool activity.

It never touches ~/.codex. Usage:

    python3 scripts/codex_compat_check.py --codex-bin "$(which codex)" [--codex-bin other/codex] [--bundle file.json]

Get older versions with: npm install --prefix /tmp/codex-0.137 @openai/codex@0.137.0
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from chatbridge.export import build_bundle  # noqa: E402
from chatbridge.models import Message, Session  # noqa: E402

SAMPLE_COMMAND = "make test-compat-probe"
SAMPLE_PATH = "/repo/compat_probe.py"


class AppServer:
    def __init__(self, codex_bin: str, codex_home: Path) -> None:
        env = {**os.environ, "CODEX_HOME": str(codex_home)}
        self.proc = subprocess.Popen(
            [codex_bin, "app-server"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=env,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
        self.lines: queue.Queue[str] = queue.Queue()
        threading.Thread(target=self._pump, daemon=True).start()
        self.next_id = 0
        self.call("initialize", {"clientInfo": {"name": "chatbridge-compat-check", "version": "1"}, "capabilities": {"experimentalApi": True}})
        self._send({"method": "initialized"})

    def _pump(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            self.lines.put(line)

    def _send(self, message: dict[str, Any]) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(message) + "\n")
        self.proc.stdin.flush()

    def call(self, method: str, params: dict[str, Any], timeout: float = 60) -> dict[str, Any]:
        self.next_id += 1
        request_id = self.next_id
        self._send({"id": request_id, "method": method, "params": params})
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                line = self.lines.get(timeout=0.5)
            except queue.Empty:
                if self.proc.poll() is not None:
                    return {"error": {"message": "app-server exited"}}
                continue
            message = json.loads(line)
            if message.get("id") == request_id and ("result" in message or "error" in message):
                return message
            if "method" in message and "id" in message:
                self._send({"id": message["id"], "error": {"code": -32601, "message": "not supported"}})
        return {"error": {"message": f"timeout waiting for {method}"}}

    def close(self) -> None:
        if self.proc.stdin:
            self.proc.stdin.close()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()


def sample_bundle(path: Path) -> Path:
    session = Session(
        source="claude",
        session_id="compat-probe",
        title="compat probe",
        project_path=str(path.parent),
        messages=[
            Message(role="user", text="Fix the compat probe"),
            Message(role="tool", tool="Bash", text=f"[Tool: Bash] $ {SAMPLE_COMMAND}\n--- output ---\n1 failed"),
            Message(role="tool", tool="Edit", text=f"[Tool: Edit] edit {SAMPLE_PATH}\n-old\n+new"),
            Message(role="assistant", text="Fixed the probe."),
        ],
    )
    path.write_text(json.dumps(build_bundle(session), ensure_ascii=False), encoding="utf-8")
    return path


def check(codex_bin: str, bundle: Path | None) -> dict[str, Any]:
    version = subprocess.run([codex_bin, "--version"], capture_output=True, text=True, check=False).stdout.strip()
    result: dict[str, Any] = {"codex": version or codex_bin}
    with tempfile.TemporaryDirectory(prefix="chatbridge-codex-") as temp_dir:
        home = Path(temp_dir)
        codex_home = home / ".codex"
        codex_home.mkdir()
        server = AppServer(codex_bin, codex_home)  # lets this Codex create its own schema
        server.call("thread/list", {})
        server.close()

        bundle_path = bundle or sample_bundle(home / "bundle.json")
        env = {**os.environ, "PYTHONPATH": str(ROOT), "CHATBRIDGE_CODEX_HOME": str(codex_home)}
        imported = subprocess.run(
            [sys.executable, "-m", "chatbridge", "--home", str(home), "native-import", "--bundle", str(bundle_path), "--to", "codex", "--project", str(home), "--apply"],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        if imported.returncode != 0:
            result["error"] = imported.stderr.strip() or imported.stdout.strip()
            return result
        thread_id = next(
            line.split("Imported into Codex session ", 1)[1].strip()
            for line in imported.stdout.splitlines()
            if line.startswith("Imported into Codex session ")
        )
        server = AppServer(codex_bin, codex_home)
        listed = server.call("thread/list", {})
        result["listed"] = any(item.get("id") == thread_id for item in (listed.get("result") or {}).get("data") or [])
        resumed = server.call("thread/resume", {"threadId": thread_id})
        result["resumed"] = "result" in resumed
        if not result["resumed"]:
            result["resumeError"] = (resumed.get("error") or {}).get("message")
        turns = server.call("thread/turns/list", {"threadId": thread_id, "limit": 50, "itemsView": "full"})
        server.close()
        text = json.dumps(turns.get("result") or {}, ensure_ascii=False)
        result["turns"] = len((turns.get("result") or {}).get("data") or [])
        if bundle is None:
            result["toolActivity"] = SAMPLE_COMMAND in text and SAMPLE_PATH in text
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--codex-bin", action="append", required=True, help="Codex executable to test (repeatable).")
    parser.add_argument("--bundle", help="Bundle to import instead of the built-in sample.")
    args = parser.parse_args(argv)
    ok = True
    for codex_bin in args.codex_bin:
        result = check(codex_bin, Path(args.bundle) if args.bundle else None)
        passed = not result.get("error") and result.get("listed") and result.get("resumed") and result.get("turns") and result.get("toolActivity", True)
        ok = ok and bool(passed)
        print(("PASS " if passed else "FAIL ") + json.dumps(result, ensure_ascii=False))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
