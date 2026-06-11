#!/usr/bin/env bash
set -euo pipefail

REPO="ylexLiao/chatbridge"
REPO_URL="${CHATBRIDGE_REPO_URL:-https://github.com/$REPO.git}"
VERSION="${CHATBRIDGE_VERSION:-latest}"
RELEASE_BASE="${CHATBRIDGE_RELEASE_BASE:-}"
PREFIX="${CHATBRIDGE_PREFIX:-}"
PREFIX_EXPLICIT=0
INSTALL_DIR="${CHATBRIDGE_INSTALL_DIR:-$HOME/.local/share/chatbridge}"
BRANCH="${CHATBRIDGE_BRANCH:-main}"
FROM_SOURCE=0
BOOTSTRAP_RUST=0
UNINSTALL=0
FORCE_REINSTALL=0
INSTALL_TMP=""
PYTHON_BIN=""
PYTHON_BIN_IS_ENV=0

if [ -n "${CHATBRIDGE_PREFIX:-}" ]; then
  PREFIX_EXPLICIT=1
fi

usage() {
  cat <<'USAGE'
Install ChatBridge.

Recommended:
  curl --http1.1 -fsSL https://github.com/ylexLiao/chatbridge/releases/latest/download/install.sh | bash

Options:
  --prefix PATH       Install wrapper into PATH/bin. Default: a writable PATH bin, then ~/.local
  --dir PATH          Install files here. Default: ~/.local/share/chatbridge
  --version VERSION   Release tag to install. Default: latest
  --from-source       Clone and build from source instead of downloading a release.
  --branch NAME       Source branch for --from-source. Default: main
  --repo URL          Source repository for --from-source.
  --bootstrap-rust    Install Rust with rustup when --from-source needs cargo.
  --force-reinstall   Replace the install directory even when it does not look like a ChatBridge install.
  --uninstall         Remove ChatBridge launcher and installed package. Keeps config/history.
  -h, --help          Show this help.
USAGE
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --prefix)
      if [ -z "${2:-}" ]; then
        echo "chatbridge install: --prefix requires a path." >&2
        exit 2
      fi
      PREFIX="$2"
      PREFIX_EXPLICIT=1
      shift 2
      ;;
    --dir)
      if [ -z "${2:-}" ]; then
        echo "chatbridge install: --dir requires a path." >&2
        exit 2
      fi
      INSTALL_DIR="$2"
      shift 2
      ;;
    --version)
      if [ -z "${2:-}" ]; then
        echo "chatbridge install: --version requires a release tag." >&2
        exit 2
      fi
      VERSION="$2"
      shift 2
      ;;
    --from-source)
      FROM_SOURCE=1
      shift
      ;;
    --branch)
      if [ -z "${2:-}" ]; then
        echo "chatbridge install: --branch requires a branch name." >&2
        exit 2
      fi
      BRANCH="$2"
      shift 2
      ;;
    --repo)
      if [ -z "${2:-}" ]; then
        echo "chatbridge install: --repo requires a repository URL." >&2
        exit 2
      fi
      REPO_URL="$2"
      shift 2
      ;;
    --bootstrap-rust)
      BOOTSTRAP_RUST=1
      shift
      ;;
    --force-reinstall)
      FORCE_REINSTALL=1
      shift
      ;;
    --uninstall)
      UNINSTALL=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "chatbridge install: unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

need() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "chatbridge install: missing dependency: $1" >&2
    exit 1
  fi
}

path_has_bin() {
  local wanted="$1"
  local entry
  local path_entries
  wanted="${wanted%/}"
  IFS=':' read -r -a path_entries <<< "${PATH:-}"
  # ${arr[@]+...} keeps bash 3.2 happy under `set -u` when the array is empty.
  for entry in ${path_entries[@]+"${path_entries[@]}"}; do
    entry="${entry%/}"
    if [ "$entry" = "$wanted" ]; then
      return 0
    fi
  done
  return 1
}

bin_is_writable_or_creatable() {
  local bin="$1"
  local parent
  if [ -d "$bin" ] && [ -w "$bin" ]; then
    return 0
  fi
  parent="$(dirname "$bin")"
  if [ ! -e "$bin" ] && [ -d "$parent" ] && [ -w "$parent" ]; then
    return 0
  fi
  return 1
}

bin_to_prefix() {
  dirname "$1"
}

ignored_path_bin() {
  case "$1" in
    /bin|/usr/bin|/sbin|/usr/sbin|/usr/local/sbin) return 0 ;;
    */node_modules/.bin) return 0 ;;
    *conda*|*Conda*|*anaconda*|*Anaconda*|*miniconda*|*Miniconda*|*mambaforge*|*Mambaforge*|*micromamba*|*Micromamba*) return 0 ;;
    *) return 1 ;;
  esac
}

choose_prefix() {
  local bin
  local path_entries
  if [ "$PREFIX_EXPLICIT" -eq 1 ]; then
    if [ -z "$PREFIX" ]; then
      echo "chatbridge install: --prefix requires a path." >&2
      exit 2
    fi
    return
  fi

  for bin in "$HOME/.local/bin" "$HOME/bin" /opt/homebrew/bin /usr/local/bin; do
    if path_has_bin "$bin" && bin_is_writable_or_creatable "$bin"; then
      PREFIX="$(bin_to_prefix "$bin")"
      return
    fi
  done

  IFS=':' read -r -a path_entries <<< "${PATH:-}"
  for bin in ${path_entries[@]+"${path_entries[@]}"}; do
    bin="${bin%/}"
    [ -n "$bin" ] || continue
    case "$bin" in
      */bin) ;;
      *) continue ;;
    esac
    ignored_path_bin "$bin" && continue
    if bin_is_writable_or_creatable "$bin"; then
      PREFIX="$(bin_to_prefix "$bin")"
      return
    fi
  done

  PREFIX="$HOME/.local"
}

python_probe() {
  # 0 = >=3.10 system interpreter, 2 = too old, 3 = >=3.10 but inside a venv/conda env.
  "$1" - <<'PY' >/dev/null 2>&1
import sys

if sys.version_info < (3, 10):
    raise SystemExit(2)
if sys.prefix != getattr(sys, "base_prefix", sys.prefix):
    raise SystemExit(3)
PY
}

python_path_is_conda() {
  case "$1" in
    *conda*|*Conda*|*anaconda*|*Anaconda*|*miniconda*|*Miniconda*|*mambaforge*|*Mambaforge*|*micromamba*|*Micromamba*|*/envs/*) return 0 ;;
    *) return 1 ;;
  esac
}

need_python() {
  local candidate resolved rc explicit="" env_fallback=""
  local candidates=(
    python3.14
    python3.13
    python3.12
    python3.11
    python3.10
    python3
    python
    /opt/homebrew/bin/python3
    /usr/local/bin/python3
  )

  if [ -n "${CHATBRIDGE_PYTHON:-}" ]; then
    explicit="$CHATBRIDGE_PYTHON"
  elif [ -n "${PYTHON:-}" ]; then
    explicit="$PYTHON"
  fi

  if [ -n "$explicit" ]; then
    if [ -x "$explicit" ] || command -v "$explicit" >/dev/null 2>&1; then
      rc=0
      python_probe "$explicit" || rc=$?
      if [ "$rc" -eq 0 ] || [ "$rc" -eq 3 ]; then
        resolved="$(command -v "$explicit" 2>/dev/null || true)"
        PYTHON_BIN="${resolved:-$explicit}"
        PYTHON_BIN_IS_ENV=0
        return
      fi
      echo "chatbridge install: CHATBRIDGE_PYTHON/PYTHON points at $explicit, which is older than Python 3.10." >&2
    else
      echo "chatbridge install: CHATBRIDGE_PYTHON/PYTHON points at $explicit, which was not found." >&2
    fi
    exit 1
  fi

  for candidate in "${candidates[@]}"; do
    [ -n "$candidate" ] || continue
    if [ -x "$candidate" ] || command -v "$candidate" >/dev/null 2>&1; then
      rc=0
      python_probe "$candidate" || rc=$?
      if [ "$rc" -ne 0 ] && [ "$rc" -ne 3 ]; then
        continue
      fi
      resolved="$(command -v "$candidate" 2>/dev/null || true)"
      resolved="${resolved:-$candidate}"
      if [ "$rc" -eq 0 ] && ! python_path_is_conda "$resolved"; then
        PYTHON_BIN="$resolved"
        PYTHON_BIN_IS_ENV=0
        return
      fi
      # >=3.10 but conda-managed or living inside a virtualenv: keep as last resort.
      if [ -z "$env_fallback" ]; then
        env_fallback="$resolved"
      fi
    fi
  done

  if [ -n "$env_fallback" ]; then
    PYTHON_BIN="$env_fallback"
    PYTHON_BIN_IS_ENV=1
    echo "chatbridge install: warning: only a conda/virtualenv Python was found: $env_fallback" >&2
    echo "chatbridge install: warning: the chatbridge wrapper will pin this interpreter; set CHATBRIDGE_PYTHON to use another one." >&2
    return
  fi

  echo "chatbridge install: Python 3.10 or newer is required." >&2
  echo "Set CHATBRIDGE_PYTHON=/path/to/python3.10+ or install a newer Python." >&2
  case "$(uname -s)" in
    Darwin) echo "macOS example: brew install python" >&2 ;;
  esac
  exit 1
}

source_install_hint() {
  echo "Try the source installer instead:" >&2
  echo "  curl --http1.1 -fsSL https://raw.githubusercontent.com/ylexLiao/chatbridge/main/install.sh | bash -s -- --from-source --bootstrap-rust" >&2
}

detect_asset() {
  local os arch
  os="$(uname -s)"
  arch="$(uname -m)"
  case "$os" in
    Darwin) os="macos" ;;
    Linux) os="linux" ;;
    *)
      echo "chatbridge install: unsupported OS for this installer: $os" >&2
      source_install_hint
      exit 1
      ;;
  esac
  case "$arch" in
    x86_64|amd64) arch="x64" ;;
    arm64|aarch64) arch="arm64" ;;
    *)
      echo "chatbridge install: unsupported architecture: $arch" >&2
      source_install_hint
      exit 1
      ;;
  esac
  printf 'chatbridge-%s-%s.tar.gz' "$os" "$arch"
}

release_url() {
  local asset="$1"
  if [ -n "$RELEASE_BASE" ]; then
    printf '%s/%s' "${RELEASE_BASE%/}" "$asset"
    return
  fi
  if [ "$VERSION" = "latest" ]; then
    printf 'https://github.com/%s/releases/latest/download/%s' "$REPO" "$asset"
  else
    printf 'https://github.com/%s/releases/download/%s/%s' "$REPO" "$VERSION" "$asset"
  fi
}

download_release_asset() {
  local url="$1"
  local output="$2"
  curl --http1.1 -fL --retry 3 --retry-delay 2 --retry-max-time 120 "$url" -o "$output"
}

write_wrapper() {
  mkdir -p "$PREFIX/bin"
  local wrapper="$PREFIX/bin/chatbridge"
  local prefix_q install_dir_q python_q
  # %q-escape interpolated paths so quotes/dollars/backticks cannot break or
  # inject into the generated wrapper. The escaped values are used in unquoted
  # assignment context, where %q escaping is exact.
  prefix_q="$(printf '%q' "$PREFIX")"
  install_dir_q="$(printf '%q' "$INSTALL_DIR")"
  python_q="$(printf '%q' "$PYTHON_BIN")"
  cat > "$wrapper" <<EOF
#!/usr/bin/env bash
set -euo pipefail
CHATBRIDGE_DEFAULT_PREFIX=$prefix_q
CHATBRIDGE_DEFAULT_INSTALL_DIR=$install_dir_q
CHATBRIDGE_PYTHON_BIN=$python_q
export CHATBRIDGE_PREFIX="\${CHATBRIDGE_PREFIX:-\$CHATBRIDGE_DEFAULT_PREFIX}"
export CHATBRIDGE_INSTALL_DIR="\${CHATBRIDGE_INSTALL_DIR:-\$CHATBRIDGE_DEFAULT_INSTALL_DIR}"
export CHATBRIDGE_INSTALLER_URL="\${CHATBRIDGE_INSTALLER_URL:-https://github.com/ylexLiao/chatbridge/releases/latest/download/install.sh}"
export PYTHONPATH="\$CHATBRIDGE_INSTALL_DIR\${PYTHONPATH:+:\$PYTHONPATH}"
exec "\$CHATBRIDGE_PYTHON_BIN" -c 'import runpy, sys; sys.path.insert(0, sys.argv.pop(1)); runpy.run_module("chatbridge", run_name="__main__", alter_sys=True)' "\$CHATBRIDGE_INSTALL_DIR" "\$@"
EOF
  chmod +x "$wrapper"
  echo "chatbridge installed: $wrapper"
  echo "Python: $PYTHON_BIN"
  if [ "$PYTHON_BIN_IS_ENV" -eq 1 ]; then
    echo "Note: the wrapper pins a conda/virtualenv Python; set CHATBRIDGE_PYTHON and re-run the installer to change it."
  fi
  echo "Run: $wrapper paths doctor"
  case ":$PATH:" in
    *":$PREFIX/bin:"*) ;;
    *) echo "Add this to PATH if needed: export PATH=\"$PREFIX/bin:\$PATH\"" ;;
  esac
}

smoke_release_binary() {
  local binary="$1"
  local output
  if [ ! -x "$binary" ]; then
    echo "chatbridge install: bundled TUI binary is missing or not executable: $binary" >&2
    exit 1
  fi
  if ! output="$(CHATBRIDGE_TUI_SMOKE=1 "$binary" 2>&1)"; then
    echo "chatbridge install: bundled TUI binary is not runnable on this machine." >&2
    printf '%s\n' "$output" >&2
    source_install_hint
    exit 1
  fi
}

safe_remove_dir() {
  local dir="${1%/}"
  local prefix="${PREFIX%/}"
  local home="${HOME%/}"
  if [ -z "$dir" ] || [ "$dir" = "/" ] || [ "$dir" = "$home" ] || [ "$dir" = "$prefix" ]; then
    echo "chatbridge uninstall: refusing to remove unsafe directory: ${1:-<empty>}" >&2
    exit 2
  fi
  case "$prefix/" in
    "$dir"/*)
      echo "chatbridge uninstall: refusing to remove $dir: it contains the install prefix $prefix" >&2
      exit 2
      ;;
  esac
  rm -rf "$dir"
}

looks_like_chatbridge_install() {
  local dir="$1"
  [ -f "$dir/chatbridge/__init__.py" ] || [ -x "$dir/bin/chatbridge-tui" ] || [ -f "$dir/bin/chatbridge-tui.exe" ]
}

remove_install_dir() {
  local label="$1"
  if [ ! -d "$INSTALL_DIR" ]; then
    return 0
  fi
  if [ "$FORCE_REINSTALL" -ne 1 ] && ! looks_like_chatbridge_install "$INSTALL_DIR"; then
    echo "chatbridge $label: refusing to remove $INSTALL_DIR: it does not look like a ChatBridge install" >&2
    echo "chatbridge $label: (expected chatbridge/__init__.py or bin/chatbridge-tui inside). Re-run with --force-reinstall to remove it anyway." >&2
    exit 2
  fi
  safe_remove_dir "$INSTALL_DIR"
}

remove_launcher_pair() {
  local wrapper="$1"
  local removed=1
  local legacy_tui
  legacy_tui="$(dirname "$wrapper")/chatbridge-tui"
  if [ -e "$wrapper" ] || [ -L "$wrapper" ]; then
    rm -f "$wrapper"
    echo "chatbridge uninstall: removed $wrapper"
    removed=0
  fi
  if [ -e "$legacy_tui" ] || [ -L "$legacy_tui" ]; then
    rm -f "$legacy_tui"
    echo "chatbridge uninstall: removed $legacy_tui"
    removed=0
  fi
  return "$removed"
}

uninstall_chatbridge() {
  local wrapper="$PREFIX/bin/chatbridge"
  local on_path removed_any=0

  # Prefer the wrapper that is actually first on PATH, then the chosen prefix —
  # but only when the user did not pin an explicit --prefix, so an uninstall
  # scoped to a test prefix can never delete an unrelated global install.
  if [ "$PREFIX_EXPLICIT" -eq 0 ]; then
    on_path="$(command -v chatbridge 2>/dev/null || true)"
    if [ -n "$on_path" ] && [ "$on_path" != "$wrapper" ]; then
      if remove_launcher_pair "$on_path"; then
        removed_any=1
      fi
    fi
  fi
  if remove_launcher_pair "$wrapper"; then
    removed_any=1
  fi
  if [ "$removed_any" -eq 0 ]; then
    echo "chatbridge uninstall: launcher not found: $wrapper"
  fi

  if [ -d "$INSTALL_DIR" ]; then
    remove_install_dir uninstall
    echo "chatbridge uninstall: removed $INSTALL_DIR"
  else
    echo "chatbridge uninstall: install directory not found: $INSTALL_DIR"
  fi

  echo "chatbridge uninstall: kept ~/.chatbridge config and source tool histories."
}

install_release() {
  need curl
  need tar
  need_python

  local asset url tmp
  if [ -n "$RELEASE_BASE" ] && [ "$VERSION" != "latest" ]; then
    echo "chatbridge install: warning: CHATBRIDGE_RELEASE_BASE is set; it overrides --version $VERSION (assets come from ${RELEASE_BASE%/})." >&2
  fi
  asset="$(detect_asset)"
  url="$(release_url "$asset")"
  tmp="$(mktemp -d)"
  INSTALL_TMP="$tmp"
  trap 'rm -rf "$INSTALL_TMP"' EXIT

  echo "chatbridge install: downloading $url"
  if ! download_release_asset "$url" "$tmp/$asset"; then
    echo "chatbridge install: release asset download failed for this platform." >&2
    source_install_hint
    exit 1
  fi

  env LC_ALL=C LANG=C tar -xzf "$tmp/$asset" -C "$tmp"
  if [ ! -d "$tmp/chatbridge" ]; then
    echo "chatbridge install: release archive did not contain a chatbridge directory." >&2
    exit 1
  fi
  smoke_release_binary "$tmp/chatbridge/bin/chatbridge-tui"

  remove_install_dir install
  mkdir -p "$(dirname "$INSTALL_DIR")"
  mv "$tmp/chatbridge" "$INSTALL_DIR"
  write_wrapper
}

install_from_source() {
  need git
  need_python
  if ! command -v cargo >/dev/null 2>&1; then
    if [ "$BOOTSTRAP_RUST" -eq 1 ]; then
      need curl
      echo "chatbridge install: installing Rust with rustup..."
      curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y
      # shellcheck disable=SC1091
      . "$HOME/.cargo/env"
    else
      echo "chatbridge install: cargo is required for --from-source." >&2
      echo "Install Rust from https://rustup.rs/ or rerun with --bootstrap-rust." >&2
      exit 1
    fi
  fi

  mkdir -p "$(dirname "$INSTALL_DIR")"
  if [ -d "$INSTALL_DIR/.git" ]; then
    echo "chatbridge install: updating $INSTALL_DIR"
    git -C "$INSTALL_DIR" fetch origin "$BRANCH"
    git -C "$INSTALL_DIR" checkout "$BRANCH"
    git -C "$INSTALL_DIR" pull --ff-only origin "$BRANCH"
  else
    if [ -e "$INSTALL_DIR" ]; then
      echo "chatbridge install: $INSTALL_DIR exists but is not a git checkout." >&2
      exit 1
    fi
    echo "chatbridge install: cloning $REPO_URL"
    git clone --branch "$BRANCH" "$REPO_URL" "$INSTALL_DIR"
  fi

  echo "chatbridge install: running tests"
  (cd "$INSTALL_DIR" && "$PYTHON_BIN" -m unittest discover -s tests)

  echo "chatbridge install: building Rust TUI"
  (cd "$INSTALL_DIR" && cargo build --manifest-path rust/chatbridge-tui/Cargo.toml --release)
  write_wrapper
}

if [ "$UNINSTALL" -eq 1 ]; then
  choose_prefix
  uninstall_chatbridge
elif [ "$FROM_SOURCE" -eq 1 ]; then
  choose_prefix
  install_from_source
else
  choose_prefix
  install_release
fi
