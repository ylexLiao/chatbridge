<div align="center">

# ChatBridge

**本地 AI 聊天历史桥接工具，支持 GitHub Copilot、Codex CLI 和 Claude Code。**

[![CI](https://img.shields.io/github/actions/workflow/status/ylexLiao/chatbridge/ci.yml?branch=main&label=CI)](https://github.com/ylexLiao/chatbridge/actions)
[![Release](https://img.shields.io/github/v/release/ylexLiao/chatbridge?include_prereleases&label=release)](https://github.com/ylexLiao/chatbridge/releases)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Platform](https://img.shields.io/badge/platform-macOS%20%7C%20Linux%20%7C%20Windows-blue)](#安装)
[![TUI](https://img.shields.io/badge/TUI-Rust-orange)](rust/chatbridge-tui)

[English](README.md) | [中文](README.zh-CN.md)

</div>

ChatBridge 是一个本地 TUI/CLI，用来浏览、交接、修复和原生导入 GitHub Copilot Chat、Codex CLI、Claude Code 的本地聊天历史。

默认情况下它不连接云服务，只读写你机器上的历史文件。执行写入前会自动备份，并尽量写成目标工具自己的历史格式，让导入结果出现在对应工具的 history / resume UI 里。

## 截图

![ChatBridge terminal dashboard](docs/assets/chatbridge-terminal.png)

## 亮点

- 浏览 Copilot、Codex、Claude Code 历史，不需要一次加载所有大 transcript。
- 生成 handoff prompt，让另一个 agent 接着做。
- 把会话原生导入到目标工具的本地历史格式。
- 修复旧导入缺失的索引、缓存、JSONL 文件。
- 诊断 VS Code、Insiders、VSCodium、Cursor、Codex 和 Claude Code 的本地历史路径。
- 把嵌入图片 base64 替换成可读的附件占位说明，避免大段乱码进入聊天记录。

## 安装

要求：Python 3.10 或更新版本。推荐的 release 安装脚本会自带预编译 Rust TUI，所以除非从源码构建，否则不需要 Rust/Cargo。

### 推荐：预编译 Release

macOS / Linux:

```bash
curl --http1.1 -fsSL https://github.com/ylexLiao/chatbridge/releases/latest/download/install.sh | bash
chatbridge
```

Windows PowerShell:

```powershell
irm https://github.com/ylexLiao/chatbridge/releases/latest/download/install.ps1 | iex
chatbridge
```

Release 安装脚本会下载对应平台的预编译 Rust TUI。普通用户不需要 Rust/Cargo。
在 macOS 和 Linux 上，安装脚本会优先把 `chatbridge` 启动器放到当前 `PATH` 中可写的 bin 目录；如果找不到，才回退到 `~/.local/bin` 并打印需要添加的 `PATH` 配置。Windows 安装脚本会在需要时更新当前 PowerShell 会话的 `PATH`，让下一行 `chatbridge` 可以直接运行。

当前预编译 release 覆盖 macOS arm64/x64、Linux arm64/x64、Windows x64。Linux release 二进制使用 musl 构建，避免要求目标机器安装较新的 glibc。其他平台可以走源码构建。

| 平台 | 预编译包 | 要求 |
| --- | --- | --- |
| macOS Apple Silicon | `chatbridge-macos-arm64.tar.gz` | Python 3.10+ |
| macOS Intel | `chatbridge-macos-x64.tar.gz` | Python 3.10+ |
| Linux arm64 | `chatbridge-linux-arm64.tar.gz` | Python 3.10+ |
| Linux x64 | `chatbridge-linux-x64.tar.gz` | Python 3.10+ |
| Windows x64 | `chatbridge-windows-x64.zip` | Python 3.10+ |

如果 `releases/latest/download/install.sh` 返回 `404`，说明 GitHub Release assets 还没有发布出来。release workflow 产出 assets 前，可以先用源码安装命令：

```bash
curl --http1.1 -fsSL https://raw.githubusercontent.com/ylexLiao/chatbridge/main/install.sh | bash -s -- --from-source --bootstrap-rust
chatbridge
```

### 通过 npm 从 GitHub 安装

```bash
npm install -g github:ylexLiao/chatbridge
chatbridge
```

这条路径会在 `postinstall` 阶段从源码构建 Rust TUI，所以需要 Cargo。它适合 npm 正式包发布前使用。

### 从源码运行

```bash
git clone https://github.com/ylexLiao/chatbridge.git
cd chatbridge
npm test
npm run build:tui
./bin/chatbridge
```

源码构建需要 Rust/Cargo。

### Homebrew

Homebrew 支持会在第一次 release checksum 可用后补上。当前模板在 [packaging/homebrew/chatbridge.rb](packaging/homebrew/chatbridge.rb)。

## 卸载

macOS / Linux:

```bash
rm -f ~/.local/bin/chatbridge ~/.local/bin/chatbridge-tui
rm -rf ~/.local/share/chatbridge
hash -r 2>/dev/null || true
```

Windows PowerShell:

```powershell
Remove-Item -Force "$HOME\.local\bin\chatbridge.cmd","$HOME\.local\bin\chatbridge.ps1" -ErrorAction SilentlyContinue
Remove-Item -Recurse -Force "$HOME\.local\share\chatbridge" -ErrorAction SilentlyContinue
```

这些命令会删除 `chatbridge` 启动器和 ChatBridge 安装包。它会保留 `~/.chatbridge/config.json`，也不会删除 Copilot、Codex、Claude Code 或 VS Code 的历史文件。
如果安装时用了自定义 `--prefix` 或 `--dir`，删除对应的 `bin/chatbridge` 启动器和安装目录即可。

也可以让安装脚本删除默认安装：

```bash
curl --http1.1 -fsSL https://github.com/ylexLiao/chatbridge/releases/latest/download/install.sh | bash -s -- --uninstall
```

重新安装并快速验证：

```bash
curl --http1.1 -fsSL https://github.com/ylexLiao/chatbridge/releases/latest/download/install.sh | bash
chatbridge paths doctor
```

## 快速开始

```bash
chatbridge paths doctor
chatbridge list --source copilot --limit 5
chatbridge list --source codex --limit 5
chatbridge list --source claude --limit 5
```

生成 handoff prompt：

```bash
chatbridge handoff --from copilot --to codex --last
```

原生导入到另一个工具：

```bash
chatbridge native-import --from codex --to claude --session <session-id> --apply
chatbridge native-import --from claude --to copilot --session <session-id> --project /path/to/repo --apply
```

`native-import` 默认是 dry-run，只有加 `--apply` 才会写入。

## 支持范围

| 工具 | 读取 | 原生导入 | 本地状态 |
| --- | --- | --- | --- |
| GitHub Copilot Chat | 支持 | 支持 | VS Code `workspaceStorage`、`chatSessions`、聊天索引、Agent Sessions cache |
| Codex CLI | 支持 | 支持 | `~/.codex/state_5.sqlite`、rollout JSONL、session/history 索引 |
| Claude Code | 支持 | 支持 | `~/.claude/history.jsonl`、project transcript JSONL |

## 路径配置

```bash
chatbridge paths doctor
chatbridge paths set --copilot-workspace-storage /path/to/workspaceStorage
chatbridge paths set --codex-home /path/to/.codex
chatbridge paths set --claude-home /path/to/.claude
chatbridge paths edit
```

配置文件位置：`~/.chatbridge/config.json`。

也支持环境变量：

```bash
CHATBRIDGE_COPILOT_WORKSPACE_STORAGE=/path/to/workspaceStorage
CHATBRIDGE_CODEX_HOME=/path/to/.codex
CHATBRIDGE_CLAUDE_HOME=/path/to/.claude
```

## 原生导入安全说明

ChatBridge 会尽量写成目标工具自己的历史格式，而不是只塞一条纯文本摘要。

- Codex：写 rollout JSONL，并更新 `state_5.sqlite`、`session_index.jsonl`、`history.jsonl`。
- Claude Code：写带 `parentUuid` 链接的 project transcript，并更新 `history.jsonl`。
- GitHub Copilot：写 VS Code `chatSessions/*.jsonl` 和 `.json`，并更新 `chat.ChatSessionStore.index` 与 Agent Sessions cache。

导入 Copilot 前请完全退出所有 VS Code 窗口。VS Code 会把聊天索引缓存在内存里，退出时可能覆盖离线写入。

修复旧导入：

```bash
chatbridge repair-codex-imports --apply
chatbridge repair-claude-imports --apply
chatbridge repair-copilot-imports --apply
```

## Copilot 本地和远程工作区

Copilot Chat 历史保存在本机 VS Code 用户数据目录里，即使项目是通过 Remote SSH、Dev Containers、WSL 或其他 `vscode-remote://` URI 打开的。

远程项目导入时，ChatBridge 仍然写本机 VS Code 的 `workspaceStorage`。workspace id 是 remote URI 的 MD5，`workspace.json` 会保留远程 folder URI：

```text
workspaceStorage/md5("vscode-remote://...")/workspace.json
workspaceStorage/md5("vscode-remote://...")/chatSessions/<session-id>.jsonl
workspaceStorage/md5("vscode-remote://...")/chatSessions/<session-id>.json
```

所以 Copilot remote import 写入的是拥有这个 remote workspace 历史的本机 VS Code profile，不是远程服务器文件系统。

## 嵌入图片

有些来源会把图片存成巨大的 `data:image/...;base64,...` 文本。ChatBridge 不会把它伪装成原生附件，因为通常已经没有可靠的原始附件文件或引用。它会替换成简短说明：

```text
[Image attachment not imported: embedded PNG data URL, approx 820.0 KB]
```

## 开发

```bash
npm test
npm run test:rust
npm run build:tui
npm pack --dry-run
```

## 许可证

MIT
