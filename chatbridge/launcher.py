from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def launch_tui(home: Path) -> int:
    binary = _find_rust_tui_binary()
    if not binary:
        configured = os.environ.get("CHATBRIDGE_TUI_BIN")
        if configured:
            print(
                f"chatbridge error: CHATBRIDGE_TUI_BIN is set to {configured!r}, but that file does not exist.\n"
                "Unset CHATBRIDGE_TUI_BIN or point it at a valid chatbridge-tui binary.",
                file=sys.stderr,
            )
        else:
            print(_rust_tui_missing_message(), file=sys.stderr)
        return 1
    env = os.environ.copy()
    root = str(Path(__file__).resolve().parents[1])
    env["PYTHONPATH"] = root + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    try:
        result = subprocess.run([str(binary), "--home", str(home)], env=env, check=False)
    except OSError as exc:
        print(_rust_tui_unusable_message(binary, exc), file=sys.stderr)
        return 1
    return int(result.returncode)


def _rust_tui_missing_message() -> str:
    return (
        "chatbridge error: Rust TUI binary was not found.\n"
        "Build it from this checkout with:\n"
        "  cargo build --manifest-path rust/chatbridge-tui/Cargo.toml --release\n"
        "Or install a ChatBridge release package that bundles chatbridge-tui."
    )


def _rust_tui_unusable_message(binary: Path, error: OSError) -> str:
    return (
        f"chatbridge error: Rust TUI binary is not runnable: {binary}\n"
        f"{error}\n"
        "Rebuild it for this platform with:\n"
        "  cargo build --manifest-path rust/chatbridge-tui/Cargo.toml --release"
    )


def _find_rust_tui_binary() -> Path | None:
    configured = os.environ.get("CHATBRIDGE_TUI_BIN")
    if configured:
        path = Path(configured).expanduser()
        return path if path.exists() else None

    suffix = ".exe" if os.name == "nt" else ""
    root = Path(__file__).resolve().parents[1]
    candidates = [
        root / "bin" / f"chatbridge-tui{suffix}",
        root / "rust" / "chatbridge-tui" / "target" / "release" / f"chatbridge-tui{suffix}",
        root / "rust" / "chatbridge-tui" / "target" / "debug" / f"chatbridge-tui{suffix}",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None
