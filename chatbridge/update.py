from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import urllib.request
from pathlib import Path


DEFAULT_UNIX_INSTALLER_URL = "https://github.com/ylexLiao/chatbridge/releases/latest/download/install.sh"
DEFAULT_WINDOWS_INSTALLER_URL = "https://github.com/ylexLiao/chatbridge/releases/latest/download/install.ps1"


def update_release_install(version: str = "latest") -> int:
    prefix = os.environ.get("CHATBRIDGE_PREFIX", "").strip()
    install_dir = os.environ.get("CHATBRIDGE_INSTALL_DIR", "").strip()
    if not prefix or not install_dir:
        raise SystemExit(
            "chatbridge update only works for release-installer installs.\n"
            "Reinstall the prebuilt release with:\n"
            "  curl --http1.1 -fsSL https://github.com/ylexLiao/chatbridge/releases/latest/download/install.sh | bash\n"
            "For npm/source installs, update with the same tool you used to install ChatBridge."
        )

    installer_url = os.environ.get(
        "CHATBRIDGE_INSTALLER_URL",
        DEFAULT_WINDOWS_INSTALLER_URL if os.name == "nt" else DEFAULT_UNIX_INSTALLER_URL,
    ).strip()
    if not installer_url:
        raise SystemExit("chatbridge update: CHATBRIDGE_INSTALLER_URL is empty.")
    release_base = os.environ.get("CHATBRIDGE_RELEASE_BASE", "").strip()
    if release_base and version != "latest":
        print(
            f"chatbridge update: warning: CHATBRIDGE_RELEASE_BASE is set; assets come from {release_base} regardless of --version {version}."
        )

    print(f"chatbridge update: installing {version} into {install_dir}")
    with tempfile.TemporaryDirectory(prefix="chatbridge-update-") as temp_dir:
        suffix = ".ps1" if os.name == "nt" else ".sh"
        installer_path = Path(temp_dir) / f"install{suffix}"
        _download_installer(installer_url, installer_path)
        if os.name == "nt":
            return _run_windows_installer(installer_path, prefix, install_dir, version)
        return _run_unix_installer(installer_path, prefix, install_dir, version)


def _download_installer(url: str, output: Path) -> None:
    local_path = Path(url).expanduser()
    if "://" not in url and local_path.exists():
        output.write_bytes(local_path.read_bytes())
    else:
        try:
            with urllib.request.urlopen(url, timeout=60) as response:
                output.write_bytes(response.read())
        except Exception as exc:  # pragma: no cover - exact network errors vary by platform
            raise SystemExit(f"chatbridge update: failed to download installer: {exc}") from exc
    _check_installer_content(url, output)


def _check_installer_content(url: str, output: Path) -> None:
    data = output.read_bytes()
    if not data.strip():
        raise SystemExit(f"chatbridge update: downloaded installer is empty: {url}")
    if output.suffix == ".sh" and not data.lstrip().startswith(b"#!"):
        raise SystemExit(f"chatbridge update: downloaded installer does not look like a shell script: {url}")
    if output.suffix == ".ps1" and b"param" not in data[:4000].lower():
        raise SystemExit(f"chatbridge update: downloaded installer does not look like a PowerShell script: {url}")


def _run_unix_installer(installer_path: Path, prefix: str, install_dir: str, version: str) -> int:
    bash = shutil.which("bash")
    if not bash:
        raise SystemExit("chatbridge update: bash is required to run the release installer.")
    env = os.environ.copy()
    env["CHATBRIDGE_PREFIX"] = prefix
    env["CHATBRIDGE_INSTALL_DIR"] = install_dir
    env["CHATBRIDGE_VERSION"] = version
    return subprocess.run(
        [
            bash,
            str(installer_path),
            "--prefix",
            prefix,
            "--dir",
            install_dir,
            "--version",
            version,
        ],
        env=env,
        check=False,
    ).returncode


def _run_windows_installer(installer_path: Path, prefix: str, install_dir: str, version: str) -> int:
    powershell = shutil.which("pwsh") or shutil.which("powershell")
    if not powershell:
        raise SystemExit("chatbridge update: PowerShell is required to run the release installer.")
    env = os.environ.copy()
    env["CHATBRIDGE_PREFIX"] = prefix
    env["CHATBRIDGE_INSTALL_DIR"] = install_dir
    env["CHATBRIDGE_VERSION"] = version
    return subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(installer_path),
            "-Prefix",
            prefix,
            "-InstallDir",
            install_dir,
            "-Version",
            version,
        ],
        env=env,
        check=False,
    ).returncode
