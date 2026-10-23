use std::env;
use std::io::{self, Read, Stdout};
use std::path::PathBuf;
use std::process::{Command, Stdio};
use std::sync::mpsc::{self, Receiver};
use std::sync::OnceLock;
use std::thread;
use std::time::{Duration, Instant};

use anyhow::{Context, Result};
use chrono::{DateTime, Local, TimeZone, Utc};
use crossterm::cursor::{Hide, Show};
use crossterm::event::{self, Event, KeyCode, KeyEvent, KeyEventKind, KeyModifiers};
use crossterm::execute;
use crossterm::terminal::{
    disable_raw_mode, enable_raw_mode, EnterAlternateScreen, LeaveAlternateScreen,
};
use ratatui::backend::CrosstermBackend;
use ratatui::layout::{Alignment, Constraint, Direction, Layout, Rect};
use ratatui::style::{Color, Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::{
    Block, BorderType, Borders, Clear, List, ListItem, ListState, Paragraph, Wrap,
};
use ratatui::{Frame, Terminal};
use serde::de::DeserializeOwned;
use serde::Deserialize;
use unicode_width::UnicodeWidthStr;

type CrosstermTerminal = Terminal<CrosstermBackend<Stdout>>;
type ApiResult<T> = std::result::Result<T, ApiError>;

const TICK_RATE: Duration = Duration::from_millis(120);
const DEFAULT_SESSION_LIMIT: usize = 50;
const SESSION_LIMIT_STEP: usize = 50;
const DEFAULT_API_TIMEOUT_SECS: u64 = 120;
const VERSION: &str = env!("CARGO_PKG_VERSION");

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ColorMode {
    Never,
    Ansi16,
    Ansi256,
    Truecolor,
}

impl ColorMode {
    fn detect() -> Self {
        Self::detect_from(|key| env::var(key).ok())
    }

    fn detect_from<F>(mut get: F) -> Self
    where
        F: FnMut(&str) -> Option<String>,
    {
        // An explicit, recognized CHATBRIDGE_COLOR setting wins over NO_COLOR:
        // the user asked this app specifically for that mode.
        match get("CHATBRIDGE_COLOR")
            .as_deref()
            .map(str::trim)
            .map(str::to_ascii_lowercase)
            .as_deref()
        {
            Some("never" | "none" | "no" | "off" | "0") => return Self::Never,
            Some("ansi16" | "16") => return Self::Ansi16,
            Some("ansi256" | "256") => return Self::Ansi256,
            Some("truecolor" | "24bit" | "rgb") => return Self::Truecolor,
            Some("auto" | "") | None => {}
            Some(_) => {}
        }

        // Per no-color.org, NO_COLOR disables color only when non-empty.
        if get("NO_COLOR")
            .map(|value| !value.is_empty())
            .unwrap_or(false)
        {
            return Self::Never;
        }

        let term = get("TERM").unwrap_or_default().to_ascii_lowercase();
        if term == "dumb" {
            return Self::Never;
        }
        let colorterm = get("COLORTERM").unwrap_or_default().to_ascii_lowercase();
        if colorterm.contains("truecolor") || colorterm.contains("24bit") {
            return Self::Truecolor;
        }
        if get("TERM_PROGRAM").as_deref() == Some("Apple_Terminal") || term.contains("256color") {
            return Self::Ansi256;
        }
        Self::Ansi16
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
struct Theme {
    mode: ColorMode,
}

impl Theme {
    fn current() -> Self {
        // Detect once per process: env-based color capability does not change
        // mid-session, and style helpers run for every span of every frame.
        static MODE: OnceLock<ColorMode> = OnceLock::new();
        Self {
            mode: *MODE.get_or_init(ColorMode::detect),
        }
    }

    fn accent_color(self) -> Option<Color> {
        match self.mode {
            ColorMode::Never => None,
            ColorMode::Ansi16 => Some(Color::Yellow),
            ColorMode::Ansi256 => Some(Color::Indexed(208)),
            ColorMode::Truecolor => Some(Color::Rgb(217, 119, 87)),
        }
    }

    fn secondary_color(self) -> Option<Color> {
        match self.mode {
            ColorMode::Never => None,
            ColorMode::Ansi16 => Some(Color::Yellow),
            ColorMode::Ansi256 => Some(Color::Indexed(214)),
            ColorMode::Truecolor => Some(Color::Rgb(246, 172, 108)),
        }
    }

    fn danger_color(self) -> Option<Color> {
        match self.mode {
            ColorMode::Never => None,
            ColorMode::Ansi16 => Some(Color::Red),
            ColorMode::Ansi256 => Some(Color::Indexed(203)),
            ColorMode::Truecolor => Some(Color::Rgb(226, 91, 91)),
        }
    }

    fn success_color(self) -> Option<Color> {
        match self.mode {
            ColorMode::Never => None,
            ColorMode::Ansi16 => Some(Color::Green),
            ColorMode::Ansi256 => Some(Color::Indexed(70)),
            ColorMode::Truecolor => Some(Color::Rgb(63, 185, 80)),
        }
    }

    fn muted_color(self) -> Option<Color> {
        match self.mode {
            ColorMode::Never | ColorMode::Ansi16 => None,
            ColorMode::Ansi256 => Some(Color::Indexed(246)),
            ColorMode::Truecolor => Some(Color::Rgb(92, 101, 112)),
        }
    }

    fn subtle_color(self) -> Option<Color> {
        match self.mode {
            ColorMode::Never | ColorMode::Ansi16 => None,
            ColorMode::Ansi256 => Some(Color::Indexed(244)),
            ColorMode::Truecolor => Some(Color::Rgb(120, 128, 138)),
        }
    }

    fn border_color(self) -> Option<Color> {
        match self.mode {
            ColorMode::Never | ColorMode::Ansi16 => None,
            ColorMode::Ansi256 => Some(Color::Indexed(245)),
            ColorMode::Truecolor => Some(Color::Rgb(142, 148, 158)),
        }
    }

    fn fg(self, color: Option<Color>) -> Style {
        match color {
            Some(color) => Style::default().fg(color),
            None => Style::default(),
        }
    }

    fn text(self) -> Style {
        Style::default()
    }

    fn muted(self) -> Style {
        self.fg(self.muted_color())
    }

    fn subtle(self) -> Style {
        self.fg(self.subtle_color())
    }

    fn border(self) -> Style {
        self.fg(self.border_color())
    }

    fn active_border(self) -> Style {
        self.accent()
    }

    fn accent(self) -> Style {
        self.fg(self.accent_color()).add_modifier(Modifier::BOLD)
    }

    fn secondary(self) -> Style {
        self.fg(self.secondary_color()).add_modifier(Modifier::BOLD)
    }

    fn success(self) -> Style {
        self.fg(self.success_color()).add_modifier(Modifier::BOLD)
    }

    fn danger(self) -> Style {
        self.fg(self.danger_color()).add_modifier(Modifier::BOLD)
    }

    fn logo_fill(self, index: usize) -> Style {
        let color = match self.mode {
            ColorMode::Never => None,
            ColorMode::Ansi16 => Some(if index % 3 == 1 {
                Color::Red
            } else {
                Color::Yellow
            }),
            ColorMode::Ansi256 => {
                const COLORS: [u8; 6] = [209, 203, 204, 209, 214, 208];
                Some(Color::Indexed(COLORS[index % COLORS.len()]))
            }
            ColorMode::Truecolor => {
                const COLORS: [(u8, u8, u8); 6] = [
                    (255, 137, 82),
                    (255, 95, 91),
                    (244, 72, 117),
                    (255, 111, 87),
                    (255, 163, 72),
                    (238, 97, 50),
                ];
                let (r, g, b) = COLORS[index % COLORS.len()];
                Some(Color::Rgb(r, g, b))
            }
        };
        self.fg(color).add_modifier(Modifier::BOLD)
    }

    fn logo_shadow(self) -> Style {
        match self.mode {
            ColorMode::Never => Style::default(),
            ColorMode::Ansi16 => self.fg(Some(Color::Red)),
            ColorMode::Ansi256 => self.fg(Some(Color::Indexed(202))),
            ColorMode::Truecolor => self.fg(Some(Color::Rgb(169, 47, 44))),
        }
    }

    fn selected(self) -> Style {
        Style::default().add_modifier(Modifier::REVERSED | Modifier::BOLD)
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Source {
    Copilot,
    Codex,
    Claude,
}

impl Source {
    fn all() -> [Source; 3] {
        [Source::Copilot, Source::Codex, Source::Claude]
    }

    fn key(self) -> &'static str {
        match self {
            Source::Copilot => "copilot",
            Source::Codex => "codex",
            Source::Claude => "claude",
        }
    }

    fn label(self) -> &'static str {
        match self {
            Source::Copilot => "GitHub Copilot",
            Source::Codex => "Codex CLI",
            Source::Claude => "Claude Code",
        }
    }

    fn short_label(self) -> &'static str {
        match self {
            Source::Copilot => "Copilot",
            Source::Codex => "Codex",
            Source::Claude => "Claude",
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ChatAction {
    Recent,
    Handoff,
    Native,
    ImportBundle,
    Paths,
}

impl ChatAction {
    fn all() -> [ChatAction; 5] {
        [
            ChatAction::Recent,
            ChatAction::Handoff,
            ChatAction::Native,
            ChatAction::ImportBundle,
            ChatAction::Paths,
        ]
    }

    fn index(self) -> usize {
        Self::all()
            .iter()
            .position(|action| *action == self)
            .unwrap_or(0)
    }

    fn label_for(self, language: Language) -> &'static str {
        if language == Language::Chinese {
            return match self {
                ChatAction::Recent => "查看最近会话",
                ChatAction::Handoff => "生成接续提示",
                ChatAction::Native => "原生导入 / 导出",
                ChatAction::ImportBundle => "导入 JSON 包",
                ChatAction::Paths => "路径诊断与设置",
            };
        }
        match self {
            ChatAction::Recent => "View Recent Sessions",
            ChatAction::Handoff => "Prompt Handoff",
            ChatAction::Native => "Native Import / Export",
            ChatAction::ImportBundle => "Import JSON Bundle",
            ChatAction::Paths => "Paths & Setup",
        }
    }

    fn description_for(self, language: Language) -> &'static str {
        if language == Language::Chinese {
            return match self {
                ChatAction::Recent => "浏览会话，回车查看内容统计（消息、工具调用、改过的文件）。",
                ChatAction::Handoff => "为另一个 Agent 生成干净的接续提示。",
                ChatAction::Native => {
                    "把会话写入另一个工具的原生历史（自动备份），或导出成 JSON 包。"
                }
                ChatAction::ImportBundle => {
                    "从 JSON 包导入（另一台机器导出的会话），可选导入到哪个工具和项目。"
                }
                ChatAction::Paths => "检查历史路径，并显示手动设置命令。",
            };
        }
        match self {
            ChatAction::Recent => "Browse sessions; Enter shows what is inside (turns, tool calls, files).",
            ChatAction::Handoff => "Create a clean continuation prompt for another agent.",
            ChatAction::Native => "Write a session into another tool's native history (backed up), or export a JSON bundle.",
            ChatAction::ImportBundle => "Import a JSON bundle exported on another machine into any tool and project.",
            ChatAction::Paths => "Inspect history paths and show manual setup commands.",
        }
    }

    fn hotkey(self) -> &'static str {
        match self {
            ChatAction::Recent => "L",
            ChatAction::Handoff => "H",
            ChatAction::Native => "N",
            ChatAction::ImportBundle => "I",
            ChatAction::Paths => "P",
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Target {
    Codex,
    Claude,
    Copilot,
    Export,
}

impl Target {
    fn available_for(source: Source) -> [Target; 3] {
        match source {
            Source::Copilot => [Target::Codex, Target::Claude, Target::Export],
            Source::Codex => [Target::Claude, Target::Copilot, Target::Export],
            Source::Claude => [Target::Codex, Target::Copilot, Target::Export],
        }
    }

    /// A bundle can go into any tool, including the one it came from on another machine.
    fn available_for_bundle() -> [Target; 3] {
        [Target::Codex, Target::Claude, Target::Copilot]
    }

    /// Whether the target stores projects as filesystem paths (vscode-remote URIs get translated).
    fn uses_filesystem_project(self) -> bool {
        matches!(self, Target::Codex | Target::Claude)
    }

    fn key(self) -> &'static str {
        match self {
            Target::Codex => "codex",
            Target::Claude => "claude",
            Target::Copilot => "copilot",
            Target::Export => "export",
        }
    }

    fn label(self) -> &'static str {
        match self {
            Target::Codex => "Codex CLI",
            Target::Claude => "Claude Code",
            Target::Copilot => "GitHub Copilot",
            Target::Export => "Export bundle (.json)",
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum PathTarget {
    CopilotWorkspaceStorage,
    CodexHome,
    ClaudeHome,
}

impl PathTarget {
    fn all() -> [PathTarget; 3] {
        [
            PathTarget::CopilotWorkspaceStorage,
            PathTarget::CodexHome,
            PathTarget::ClaudeHome,
        ]
    }

    fn api_arg(self) -> &'static str {
        match self {
            PathTarget::CopilotWorkspaceStorage => "--copilot-workspace-storage",
            PathTarget::CodexHome => "--codex-home",
            PathTarget::ClaudeHome => "--claude-home",
        }
    }

    fn label_for(self, language: Language) -> &'static str {
        if language == Language::Chinese {
            return match self {
                PathTarget::CopilotWorkspaceStorage => "Copilot workspaceStorage",
                PathTarget::CodexHome => "Codex 主目录",
                PathTarget::ClaudeHome => "Claude 主目录",
            };
        }
        match self {
            PathTarget::CopilotWorkspaceStorage => "Copilot workspaceStorage",
            PathTarget::CodexHome => "Codex home",
            PathTarget::ClaudeHome => "Claude home",
        }
    }

    fn hint_for(self, language: Language) -> &'static str {
        if language == Language::Chinese {
            return match self {
                PathTarget::CopilotWorkspaceStorage => "VS Code/Cursor 的 workspaceStorage 目录",
                PathTarget::CodexHome => "通常是 ~/.codex",
                PathTarget::ClaudeHome => "通常是 ~/.claude",
            };
        }
        match self {
            PathTarget::CopilotWorkspaceStorage => "VS Code/Cursor workspaceStorage directory",
            PathTarget::CodexHome => "Usually ~/.codex",
            PathTarget::ClaudeHome => "Usually ~/.claude",
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum View {
    Home,
    Loading,
    Sessions,
    SessionPreview,
    Target,
    ConfirmNative,
    ConfirmDuplicate,
    PathSetup,
    Result,
    Bundles,
    BundlePath,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Language {
    English,
    Chinese,
}

impl Language {
    fn toggle(self) -> Self {
        match self {
            Language::English => Language::Chinese,
            Language::Chinese => Language::English,
        }
    }

    fn label(self) -> &'static str {
        match self {
            Language::English => "EN",
            Language::Chinese => "中文",
        }
    }

    fn from_env() -> Self {
        Self::detect_from(|key| env::var(key).ok())
    }

    /// CHATBRIDGE_LANG wins; otherwise the usual locale variables (zh_* -> Chinese).
    fn detect_from<F>(mut get: F) -> Self
    where
        F: FnMut(&str) -> Option<String>,
    {
        if let Some(value) = get("CHATBRIDGE_LANG") {
            let value = value.trim().to_ascii_lowercase();
            if value.starts_with("zh") || value == "cn" || value == "chinese" {
                return Language::Chinese;
            }
            if value.starts_with("en") {
                return Language::English;
            }
        }
        for key in ["LC_ALL", "LC_MESSAGES", "LANG", "LANGUAGE"] {
            if let Some(value) = get(key) {
                let value = value.trim().to_ascii_lowercase();
                if value.is_empty() || value == "c" || value == "posix" {
                    continue;
                }
                return if value.starts_with("zh") {
                    Language::Chinese
                } else {
                    Language::English
                };
            }
        }
        Language::English
    }
}

fn text(language: Language, english: &'static str, chinese: &'static str) -> &'static str {
    match language {
        Language::English => english,
        Language::Chinese => chinese,
    }
}

#[derive(Clone, Debug, Deserialize, PartialEq, Eq)]
pub struct SessionRow {
    pub source: String,
    #[serde(rename = "sourceLabel")]
    pub source_label: String,
    #[serde(rename = "sessionId")]
    pub session_id: String,
    pub title: String,
    #[serde(rename = "projectPath")]
    pub project_path: Option<String>,
    #[serde(rename = "createdAt")]
    pub created_at: Option<serde_json::Value>,
    #[serde(rename = "updatedAt")]
    pub updated_at: Option<serde_json::Value>,
    #[serde(rename = "rawPath")]
    pub raw_path: Option<String>,
    pub scope: String,
}

impl SessionRow {
    fn start_time_label(&self) -> String {
        format_time_label(&self.created_at)
    }

    fn updated_time_label(&self) -> String {
        format_time_label(&self.updated_at)
    }

    fn project_label(&self) -> String {
        self.project_path.as_deref().unwrap_or("-").to_string()
    }

    fn scope_label(&self) -> String {
        if !self.scope.trim().is_empty() {
            return self.scope.clone();
        }
        match self.source.as_str() {
            "copilot" => "LOCAL".to_string(),
            "codex" => "CODEX".to_string(),
            "claude" => "CLAUDE".to_string(),
            value => value.to_uppercase(),
        }
    }

    fn matches_filter(&self, query: &str) -> bool {
        let needle = query.trim().to_lowercase();
        if needle.is_empty() {
            return true;
        }
        [
            self.title.as_str(),
            self.session_id.as_str(),
            self.project_path.as_deref().unwrap_or_default(),
            self.raw_path.as_deref().unwrap_or_default(),
            self.scope.as_str(),
            self.source.as_str(),
            self.source_label.as_str(),
        ]
        .join(" ")
        .to_lowercase()
        .contains(&needle)
    }
}

#[derive(Clone, Debug, Deserialize, PartialEq, Eq)]
pub struct ApiError {
    pub kind: String,
    pub message: String,
    #[serde(rename = "nextTitle")]
    pub next_title: Option<String>,
}

#[derive(Debug, Deserialize)]
struct ApiEnvelope<T> {
    ok: bool,
    data: Option<T>,
    kind: Option<String>,
    message: Option<String>,
    #[serde(rename = "nextTitle")]
    next_title: Option<String>,
}

#[derive(Debug, Deserialize)]
pub struct SessionsData {
    sessions: Vec<SessionRow>,
    loaded: usize,
    total: usize,
    limit: usize,
    #[serde(rename = "hasMore")]
    has_more: bool,
}

#[derive(Debug, Deserialize)]
struct TextData {
    text: String,
}

#[derive(Clone, Debug, Default, Deserialize, PartialEq, Eq)]
pub struct BundleStats {
    #[serde(default)]
    messages: Option<u64>,
    #[serde(default)]
    user: Option<u64>,
    #[serde(default)]
    assistant: Option<u64>,
    #[serde(default, rename = "toolCalls")]
    tool_calls: Option<u64>,
    #[serde(default, rename = "approxTokens")]
    approx_tokens: Option<u64>,
}

#[derive(Clone, Debug, Default, Deserialize, PartialEq, Eq)]
pub struct BundleRow {
    pub path: String,
    #[serde(default)]
    pub name: String,
    #[serde(default)]
    pub size: u64,
    #[serde(default)]
    pub modified: Option<serde_json::Value>,
    #[serde(default, rename = "exportedAt")]
    pub exported_at: Option<serde_json::Value>,
    #[serde(default)]
    pub source: Option<String>,
    #[serde(default, rename = "sessionId")]
    pub session_id: Option<String>,
    #[serde(default)]
    pub title: Option<String>,
    #[serde(default, rename = "projectPath")]
    pub project_path: Option<String>,
    #[serde(default)]
    pub stats: Option<BundleStats>,
}

impl BundleRow {
    fn from_path(path: &str) -> Self {
        let name = std::path::Path::new(path)
            .file_name()
            .map(|name| name.to_string_lossy().to_string())
            .unwrap_or_else(|| path.to_string());
        BundleRow {
            path: path.to_string(),
            name,
            ..BundleRow::default()
        }
    }

    fn title_label(&self) -> String {
        self.title
            .clone()
            .filter(|title| !title.trim().is_empty())
            .unwrap_or_else(|| self.name.clone())
    }

    fn source_label(&self) -> String {
        match self.source.as_deref() {
            Some("copilot") => "Copilot".to_string(),
            Some("codex") => "Codex".to_string(),
            Some("claude") => "Claude".to_string(),
            Some(other) if !other.is_empty() => other.to_string(),
            _ => "?".to_string(),
        }
    }
}

#[derive(Debug, Deserialize)]
pub struct BundlesData {
    bundles: Vec<BundleRow>,
    #[serde(default)]
    dirs: Vec<String>,
    #[serde(default, rename = "exportDir")]
    export_dir: String,
}

#[derive(Clone, Debug, Default, Deserialize, PartialEq)]
pub struct ImportEstimate {
    #[serde(default)]
    level: String,
    #[serde(default)]
    budget: Option<u64>,
    #[serde(default)]
    messages: u64,
    #[serde(default, rename = "approxTokens")]
    approx_tokens: u64,
    #[serde(default)]
    trimmed: bool,
    #[serde(default)]
    notes: Vec<String>,
}

#[derive(Clone, Debug, Default, Deserialize, PartialEq)]
pub struct PreviewData {
    #[serde(default, rename = "sourceLabel")]
    source_label: String,
    #[serde(default, rename = "sessionId")]
    session_id: String,
    #[serde(default)]
    title: String,
    #[serde(default, rename = "projectPath")]
    project_path: Option<String>,
    #[serde(default, rename = "localProject")]
    local_project: Option<String>,
    #[serde(default, rename = "projectExists")]
    project_exists: bool,
    #[serde(default, rename = "createdAt")]
    created_at: Option<serde_json::Value>,
    #[serde(default, rename = "updatedAt")]
    updated_at: Option<serde_json::Value>,
    #[serde(default)]
    stats: serde_json::Value,
    #[serde(default, rename = "filesTouched")]
    files_touched: Vec<String>,
    #[serde(default, rename = "firstPrompt")]
    first_prompt: Option<String>,
    #[serde(default, rename = "lastPrompt")]
    last_prompt: Option<String>,
    #[serde(default, rename = "lastReply")]
    last_reply: Option<String>,
    #[serde(default, rename = "recentTools")]
    recent_tools: Vec<String>,
    #[serde(default, rename = "importEstimate")]
    import_estimate: Option<ImportEstimate>,
    #[serde(default, rename = "transcriptMissing")]
    transcript_missing: bool,
}

impl PreviewData {
    fn stat(&self, key: &str) -> u64 {
        self.stats
            .get(key)
            .and_then(|value| value.as_u64())
            .unwrap_or(0)
    }

    fn tool_breakdown(&self, max: usize) -> String {
        let Some(tools) = self.stats.get("tools").and_then(|value| value.as_object()) else {
            return String::new();
        };
        tools
            .iter()
            .take(max)
            .map(|(name, count)| format!("{name} {}", count.as_u64().unwrap_or(0)))
            .collect::<Vec<_>>()
            .join(", ")
    }
}

#[derive(Clone, Debug, Deserialize, PartialEq, Eq)]
pub struct SourceStatus {
    pub source: String,
    #[serde(default)]
    pub path: String,
    pub exists: bool,
}

#[derive(Debug, Deserialize)]
pub struct StatusData {
    sources: Vec<SourceStatus>,
}

/// What a native import reads from: a local session of the current source, or a bundle file.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum ImportRequest {
    Session { source: Source, session_id: String },
    Bundle { path: String },
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum PreviewPurpose {
    /// Show the import preview modal, then continue to target selection.
    Import,
    /// Show the session detail in the result view.
    Detail,
}

#[derive(Debug)]
pub enum UiCommand {
    None,
    Quit,
    LoadSessions {
        source: Source,
        action: ChatAction,
        limit: usize,
        preserve_filter: bool,
    },
    BuildHandoff {
        source: Source,
        target: Target,
        session_id: String,
    },
    NativeImport {
        request: ImportRequest,
        target: Target,
        apply: bool,
        allow_duplicate: bool,
        project: Option<String>,
    },
    ExportSession {
        source: Source,
        session_id: String,
    },
    LoadBundles,
    LoadPreview {
        request: ImportRequest,
        purpose: PreviewPurpose,
    },
    CopyText(String),
    PathsDoctor,
    SetPath {
        target: PathTarget,
        value: String,
    },
}

#[derive(Debug)]
pub enum WorkerResult {
    Sessions(ApiResult<SessionsData>),
    Handoff(ApiResult<String>),
    Native(ApiResult<String>),
    Export(ApiResult<String>),
    Bundles(ApiResult<BundlesData>),
    Preview(PreviewPurpose, ApiResult<Box<PreviewData>>),
    Status(ApiResult<StatusData>),
    Paths(ApiResult<String>),
    PathSet(ApiResult<String>),
}

#[derive(Debug)]
pub struct App {
    home: PathBuf,
    view: View,
    source_index: usize,
    action_index: usize,
    target_index: usize,
    selected_index: usize,
    confirm_index: usize,
    preview_confirm_index: usize,
    pending_action: ChatAction,
    sessions: Vec<SessionRow>,
    session_limit: usize,
    session_loaded: usize,
    session_total: usize,
    session_has_more: bool,
    preserve_filter_after_load: bool,
    filter: String,
    filter_input: String,
    filtering: bool,
    path_index: usize,
    path_input: String,
    loading_message: String,
    result_title: String,
    result_text: String,
    result_scroll: u16,
    result_back_view: View,
    loading_back_view: View,
    duplicate_message: String,
    duplicate_next_title: String,
    language: Language,
    worker_rx: Option<Receiver<WorkerResult>>,
    status_rx: Option<Receiver<WorkerResult>>,
    tick: usize,
    /// Bundle chosen in the Import JSON Bundle flow; None means "the selected session".
    bundle_origin: Option<BundleRow>,
    bundles: Vec<BundleRow>,
    bundle_dirs: Vec<String>,
    export_dir: String,
    bundle_index: usize,
    bundle_input: String,
    preview: Option<PreviewData>,
    preview_error: Option<String>,
    project_override: Option<String>,
    editing_project: bool,
    project_input: String,
    status_message: Option<String>,
    source_status: Vec<SourceStatus>,
}

impl App {
    pub fn new(home: PathBuf) -> Self {
        Self {
            home,
            view: View::Home,
            source_index: 0,
            action_index: 0,
            target_index: 0,
            selected_index: 0,
            confirm_index: 0,
            preview_confirm_index: 0,
            pending_action: ChatAction::Recent,
            sessions: Vec::new(),
            session_limit: DEFAULT_SESSION_LIMIT,
            session_loaded: 0,
            session_total: 0,
            session_has_more: false,
            preserve_filter_after_load: false,
            filter: String::new(),
            filter_input: String::new(),
            filtering: false,
            path_index: 0,
            path_input: String::new(),
            loading_message: "Ready".to_string(),
            result_title: "Result".to_string(),
            result_text: String::new(),
            result_scroll: 0,
            result_back_view: View::Home,
            loading_back_view: View::Home,
            duplicate_message: String::new(),
            duplicate_next_title: String::new(),
            language: Language::English,
            worker_rx: None,
            status_rx: None,
            tick: 0,
            bundle_origin: None,
            bundles: Vec::new(),
            bundle_dirs: Vec::new(),
            export_dir: String::new(),
            bundle_index: 0,
            bundle_input: String::new(),
            preview: None,
            preview_error: None,
            project_override: None,
            editing_project: false,
            project_input: String::new(),
            status_message: None,
            source_status: Vec::new(),
        }
    }

    pub fn view(&self) -> &View {
        &self.view
    }

    pub fn loading_message(&self) -> &str {
        &self.loading_message
    }

    fn source(&self) -> Source {
        Source::all()[self.source_index]
    }

    fn action(&self) -> ChatAction {
        ChatAction::all()[self.action_index]
    }

    fn targets(&self) -> [Target; 3] {
        if self.bundle_origin.is_some() {
            Target::available_for_bundle()
        } else {
            Target::available_for(self.source())
        }
    }

    fn target(&self) -> Target {
        let targets = self.targets();
        targets[self.target_index % targets.len()]
    }

    /// The import source for the current flow: the chosen bundle, else the selected session.
    fn import_request(&self) -> Option<ImportRequest> {
        if let Some(bundle) = &self.bundle_origin {
            return Some(ImportRequest::Bundle {
                path: bundle.path.clone(),
            });
        }
        self.selected_session()
            .map(|session| ImportRequest::Session {
                source: self.source(),
                session_id: session.session_id.clone(),
            })
    }

    fn import_title(&self) -> String {
        if let Some(preview) = self.preview.as_ref().filter(|p| !p.title.is_empty()) {
            return preview.title.clone();
        }
        if let Some(bundle) = &self.bundle_origin {
            return bundle.title_label();
        }
        self.selected_session()
            .map(|session| session.title.clone())
            .unwrap_or_else(|| "-".to_string())
    }

    /// Project the import will be written into, mirroring the Python resolution.
    fn import_project(&self) -> Option<String> {
        if let Some(project) = &self.project_override {
            return Some(project.clone());
        }
        if let Some(preview) = &self.preview {
            if self.target().uses_filesystem_project() {
                if let Some(local) = preview.local_project.as_ref().filter(|p| !p.is_empty()) {
                    return Some(local.clone());
                }
            }
            if let Some(project) = preview.project_path.as_ref().filter(|p| !p.is_empty()) {
                return Some(project.clone());
            }
        }
        if let Some(bundle) = &self.bundle_origin {
            return bundle.project_path.clone().filter(|p| !p.is_empty());
        }
        self.selected_session()
            .and_then(|session| session.project_path.clone())
            .filter(|p| !p.is_empty())
    }

    /// Back view for session-scoped results: the bundle list in the bundle flow.
    fn flow_back_view(&self) -> View {
        if self.bundle_origin.is_some() {
            View::Bundles
        } else {
            View::Sessions
        }
    }

    fn reset_import_state(&mut self) {
        self.preview = None;
        self.preview_error = None;
        self.project_override = None;
        self.editing_project = false;
        self.project_input.clear();
    }

    fn path_target(&self) -> PathTarget {
        let targets = PathTarget::all();
        targets[self.path_index % targets.len()]
    }

    fn selected_session(&self) -> Option<&SessionRow> {
        self.selected_session_index()
            .and_then(|index| self.sessions.get(index))
    }

    fn selected_session_index(&self) -> Option<usize> {
        if self.filter.trim().is_empty() {
            return self
                .sessions
                .get(self.selected_index)
                .map(|_| self.selected_index);
        }
        self.sessions
            .iter()
            .enumerate()
            .filter(|(_, session)| session.matches_filter(&self.filter))
            .nth(self.selected_index)
            .map(|(index, _)| index)
    }

    fn filtered_indices(&self) -> Vec<usize> {
        if self.filter.trim().is_empty() {
            return (0..self.sessions.len()).collect();
        }
        self.sessions
            .iter()
            .enumerate()
            .filter_map(|(index, session)| session.matches_filter(&self.filter).then_some(index))
            .collect()
    }

    fn filtered_count(&self) -> usize {
        if self.filter.trim().is_empty() {
            return self.sessions.len();
        }
        self.sessions
            .iter()
            .filter(|session| session.matches_filter(&self.filter))
            .count()
    }

    fn selectable_session_rows(&self) -> usize {
        self.filtered_count() + usize::from(self.session_has_more)
    }

    fn is_load_more_selected(&self) -> bool {
        self.session_has_more && self.selected_index >= self.filtered_count()
    }

    fn clamp_selection(&mut self) {
        let total = self.selectable_session_rows();
        if total == 0 {
            self.selected_index = 0;
        } else if self.selected_index >= total {
            self.selected_index = total - 1;
        }
    }
}

pub fn run() -> Result<()> {
    if env::var("CHATBRIDGE_TUI_SMOKE").as_deref() == Ok("1") {
        println!("ChatBridge TUI");
        println!("Rust ratatui TUI");
        println!("Claude Orange theme");
        println!("GitHub Copilot");
        println!("Codex CLI");
        println!("Claude Code");
        println!("Prompt Handoff");
        println!("Native Import");
        println!("↑/↓ move  Enter select");
        return Ok(());
    }

    let home = parse_home_arg();
    let mut app = App::new(home);
    app.language = Language::from_env();
    start_status_load(&mut app);
    let mut terminal = setup_terminal()?;
    let result = run_loop(&mut terminal, app);
    restore_terminal(&mut terminal)?;
    result
}

/// Probe which history folders exist, in the background, for the dashboard.
fn start_status_load(app: &mut App) {
    let home = app.home.clone();
    let (tx, rx) = mpsc::channel();
    app.status_rx = Some(rx);
    thread::spawn(move || {
        let _ = tx.send(WorkerResult::Status(api_status(&home)));
    });
}

fn drain_status(app: &mut App) {
    let Some(rx) = app.status_rx.take() else {
        return;
    };
    match rx.try_recv() {
        Ok(WorkerResult::Status(Ok(data))) => app.source_status = data.sources,
        Ok(_) | Err(mpsc::TryRecvError::Disconnected) => {}
        Err(mpsc::TryRecvError::Empty) => app.status_rx = Some(rx),
    }
}

fn parse_home_arg() -> PathBuf {
    let mut args = env::args().skip(1);
    while let Some(arg) = args.next() {
        if arg == "--home" {
            match args.next() {
                Some(value) => return PathBuf::from(value),
                None => {
                    eprintln!("chatbridge-tui: --home requires a value");
                    std::process::exit(2);
                }
            }
        }
    }
    env::var_os("HOME")
        .or_else(|| env::var_os("USERPROFILE"))
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("."))
}

fn setup_terminal() -> Result<CrosstermTerminal> {
    enable_raw_mode().context("enable raw mode")?;
    let mut stdout = io::stdout();
    execute!(stdout, EnterAlternateScreen, Hide).context("enter alternate screen")?;
    Terminal::new(CrosstermBackend::new(stdout)).context("create terminal")
}

fn restore_terminal(terminal: &mut CrosstermTerminal) -> Result<()> {
    disable_raw_mode().context("disable raw mode")?;
    execute!(terminal.backend_mut(), LeaveAlternateScreen, Show)
        .context("leave alternate screen")?;
    Ok(())
}

fn run_loop(terminal: &mut CrosstermTerminal, mut app: App) -> Result<()> {
    loop {
        drain_worker(&mut app);
        drain_status(&mut app);
        terminal.draw(|frame| render(frame, &mut app))?;
        if event::poll(TICK_RATE)? {
            if let Event::Key(key) = event::read()? {
                if key.kind != KeyEventKind::Press {
                    continue;
                }
                let command = handle_key(&mut app, key);
                match command {
                    UiCommand::Quit => return Ok(()),
                    UiCommand::None => {}
                    UiCommand::CopyText(text) => {
                        let (payload, truncated) = osc52_payload(&text);
                        let written =
                            io::Write::write_all(terminal.backend_mut(), payload.as_bytes())
                                .and_then(|_| io::Write::flush(terminal.backend_mut()));
                        app.status_message = Some(match (written, truncated) {
                            (Err(error), _) => format!("Copy failed: {error}"),
                            (Ok(()), false) => text_for(
                                app.language,
                                "Copied to clipboard (OSC 52; in tmux enable set-clipboard).",
                                "已复制到剪贴板（OSC 52；tmux 需开启 set-clipboard）。",
                            ),
                            (Ok(()), true) => text_for(
                                app.language,
                                "Copied the first 48 KB (terminal clipboard limit).",
                                "已复制前 48 KB（终端剪贴板上限）。",
                            ),
                        });
                    }
                    other => begin_command(&mut app, other),
                }
            }
        }
        app.tick = app.tick.wrapping_add(1);
    }
}

/// Raw bytes copied through OSC 52 (base64 grows them by a third).
const OSC52_MAX_BYTES: usize = 48 * 1024;

/// OSC 52 "set clipboard" escape sequence for text, capped at OSC52_MAX_BYTES.
fn osc52_payload(text: &str) -> (String, bool) {
    let mut end = text.len().min(OSC52_MAX_BYTES);
    while !text.is_char_boundary(end) {
        end -= 1;
    }
    (
        format!("\x1b]52;c;{}\x07", base64_encode(&text.as_bytes()[..end])),
        end < text.len(),
    )
}

fn base64_encode(bytes: &[u8]) -> String {
    const TABLE: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut out = String::with_capacity(bytes.len().div_ceil(3) * 4);
    for chunk in bytes.chunks(3) {
        let b = [
            chunk[0],
            *chunk.get(1).unwrap_or(&0),
            *chunk.get(2).unwrap_or(&0),
        ];
        let n = (u32::from(b[0]) << 16) | (u32::from(b[1]) << 8) | u32::from(b[2]);
        out.push(TABLE[(n >> 18) as usize & 63] as char);
        out.push(TABLE[(n >> 12) as usize & 63] as char);
        out.push(if chunk.len() > 1 {
            TABLE[(n >> 6) as usize & 63] as char
        } else {
            '='
        });
        out.push(if chunk.len() > 2 {
            TABLE[n as usize & 63] as char
        } else {
            '='
        });
    }
    out
}

pub fn handle_key(app: &mut App, key: KeyEvent) -> UiCommand {
    if key.modifiers.contains(KeyModifiers::CONTROL)
        && matches!(key.code, KeyCode::Char('c') | KeyCode::Char('C'))
    {
        return UiCommand::Quit;
    }
    if app.filtering {
        return handle_filter_key(app, key);
    }
    if app.editing_project {
        return handle_project_edit_key(app, key);
    }
    app.status_message = None;
    // The language toggle stays out of text-input views so 't' remains typeable.
    let typing = matches!(app.view, View::PathSetup | View::BundlePath);
    if !typing && matches!(key.code, KeyCode::Char('t') | KeyCode::Char('T')) {
        app.language = app.language.toggle();
        return UiCommand::None;
    }
    match app.view {
        View::Home => handle_home_key(app, key),
        View::Sessions => handle_sessions_key(app, key),
        View::SessionPreview => handle_session_preview_key(app, key),
        View::Target => handle_target_key(app, key),
        View::ConfirmNative => handle_confirm_native_key(app, key),
        View::ConfirmDuplicate => handle_confirm_duplicate_key(app, key),
        View::PathSetup => handle_path_setup_key(app, key),
        View::Result => handle_result_key(app, key),
        View::Loading => handle_loading_key(app, key),
        View::Bundles => handle_bundles_key(app, key),
        View::BundlePath => handle_bundle_path_key(app, key),
    }
}

fn text_for(language: Language, english: &str, chinese: &str) -> String {
    match language {
        Language::English => english.to_string(),
        Language::Chinese => chinese.to_string(),
    }
}

fn handle_loading_key(app: &mut App, key: KeyEvent) -> UiCommand {
    match key.code {
        KeyCode::Esc | KeyCode::Char('q') => {
            cancel_loading(app);
            UiCommand::None
        }
        _ => UiCommand::None,
    }
}

fn cancel_loading(app: &mut App) {
    // Drop the receiver so a late worker result is never applied.
    app.worker_rx = None;
    let back_view = app.loading_back_view;
    show_result(
        app,
        text(app.language, "Cancelled", "已取消"),
        text(
            app.language,
            "Operation cancelled.\nNote: an in-flight apply may still finish in the background; check the target tool before retrying.",
            "操作已取消。\n注意：正在执行的写入可能仍会在后台完成；重试前请先检查目标工具。",
        ),
        back_view,
    );
}

fn show_result(app: &mut App, title: impl Into<String>, text: impl Into<String>, back_view: View) {
    app.result_title = title.into();
    app.result_text = text.into();
    app.result_scroll = 0;
    app.result_back_view = back_view;
    app.view = View::Result;
}

fn open_path_setup(app: &mut App) {
    app.action_index = ChatAction::all()
        .iter()
        .position(|action| *action == ChatAction::Paths)
        .unwrap_or(0);
    app.view = View::PathSetup;
    app.path_index = app.path_index.min(PathTarget::all().len() - 1);
    app.path_input.clear();
}

fn handle_home_key(app: &mut App, key: KeyEvent) -> UiCommand {
    match key.code {
        KeyCode::Char('q') => UiCommand::Quit,
        KeyCode::Tab => {
            app.source_index = (app.source_index + 1) % Source::all().len();
            app.target_index = 0;
            UiCommand::None
        }
        KeyCode::Up => {
            app.action_index = app.action_index.saturating_sub(1);
            UiCommand::None
        }
        KeyCode::Down => {
            app.action_index = (app.action_index + 1).min(ChatAction::all().len() - 1);
            UiCommand::None
        }
        KeyCode::Char('l') | KeyCode::Char('L') => start_action(app, ChatAction::Recent),
        KeyCode::Char('h') | KeyCode::Char('H') => start_action(app, ChatAction::Handoff),
        KeyCode::Char('n') | KeyCode::Char('N') => start_action(app, ChatAction::Native),
        KeyCode::Char('i') | KeyCode::Char('I') => start_action(app, ChatAction::ImportBundle),
        KeyCode::Char('p') | KeyCode::Char('P') => start_action(app, ChatAction::Paths),
        KeyCode::Enter => start_action(app, app.action()),
        _ => UiCommand::None,
    }
}

fn start_action(app: &mut App, action: ChatAction) -> UiCommand {
    app.action_index = action.index();
    app.bundle_origin = None;
    app.reset_import_state();
    match action {
        ChatAction::Recent | ChatAction::Handoff | ChatAction::Native => UiCommand::LoadSessions {
            source: app.source(),
            action,
            limit: DEFAULT_SESSION_LIMIT,
            preserve_filter: false,
        },
        ChatAction::ImportBundle => UiCommand::LoadBundles,
        ChatAction::Paths => {
            open_path_setup(app);
            UiCommand::None
        }
    }
}

fn handle_sessions_key(app: &mut App, key: KeyEvent) -> UiCommand {
    match key.code {
        KeyCode::Esc | KeyCode::Char('q') => {
            app.view = View::Home;
            UiCommand::None
        }
        KeyCode::Char('/') => {
            app.filtering = true;
            app.filter_input = app.filter.clone();
            UiCommand::None
        }
        KeyCode::Up => {
            app.selected_index = app.selected_index.saturating_sub(1);
            UiCommand::None
        }
        KeyCode::Down => {
            let total = app.selectable_session_rows();
            if total > 0 {
                app.selected_index = (app.selected_index + 1).min(total - 1);
            }
            UiCommand::None
        }
        KeyCode::Enter => {
            if app.is_load_more_selected() {
                return UiCommand::LoadSessions {
                    source: app.source(),
                    action: app.pending_action,
                    limit: app.session_limit + SESSION_LIMIT_STEP,
                    preserve_filter: true,
                };
            }
            match app.pending_action {
                ChatAction::Recent => match app.import_request() {
                    Some(request) => UiCommand::LoadPreview {
                        request,
                        purpose: PreviewPurpose::Detail,
                    },
                    None => UiCommand::None,
                },
                ChatAction::Handoff => {
                    app.view = View::Target;
                    app.target_index = 0;
                    UiCommand::None
                }
                ChatAction::Native => {
                    app.reset_import_state();
                    match app.import_request() {
                        Some(request) => UiCommand::LoadPreview {
                            request,
                            purpose: PreviewPurpose::Import,
                        },
                        None => UiCommand::None,
                    }
                }
                ChatAction::ImportBundle | ChatAction::Paths => UiCommand::None,
            }
        }
        _ => UiCommand::None,
    }
}

fn handle_session_preview_key(app: &mut App, key: KeyEvent) -> UiCommand {
    match key.code {
        KeyCode::Esc | KeyCode::Char('q') | KeyCode::Char('n') | KeyCode::Char('N') => {
            app.view = app.flow_back_view();
            UiCommand::None
        }
        KeyCode::Char('y') | KeyCode::Char('Y') => {
            app.target_index = 0;
            app.view = View::Target;
            UiCommand::None
        }
        KeyCode::Left | KeyCode::Right => {
            app.preview_confirm_index = 1 - app.preview_confirm_index.min(1);
            UiCommand::None
        }
        KeyCode::Enter => {
            if app.preview_confirm_index == 0 {
                app.target_index = 0;
                app.view = View::Target;
            } else {
                app.view = app.flow_back_view();
            }
            UiCommand::None
        }
        _ => UiCommand::None,
    }
}

fn handle_target_key(app: &mut App, key: KeyEvent) -> UiCommand {
    match key.code {
        KeyCode::Esc | KeyCode::Char('q') => {
            app.view = app.flow_back_view();
            UiCommand::None
        }
        KeyCode::Left => {
            let target_count = app.targets().len();
            app.target_index = (app.target_index + target_count - 1) % target_count;
            UiCommand::None
        }
        KeyCode::Right => {
            let target_count = app.targets().len();
            app.target_index = (app.target_index + 1) % target_count;
            UiCommand::None
        }
        KeyCode::Enter => {
            if app.target() == Target::Export {
                let Some(session) = app.selected_session() else {
                    return UiCommand::None;
                };
                return UiCommand::ExportSession {
                    source: app.source(),
                    session_id: session.session_id.clone(),
                };
            }
            if app.pending_action == ChatAction::Handoff && app.bundle_origin.is_none() {
                let Some(session) = app.selected_session() else {
                    return UiCommand::None;
                };
                return UiCommand::BuildHandoff {
                    source: app.source(),
                    target: app.target(),
                    session_id: session.session_id.clone(),
                };
            }
            app.confirm_index = 0;
            app.view = View::ConfirmNative;
            UiCommand::None
        }
        _ => UiCommand::None,
    }
}

fn native_import_command(app: &App, apply: bool, allow_duplicate: bool) -> UiCommand {
    let Some(request) = app.import_request() else {
        return UiCommand::None;
    };
    UiCommand::NativeImport {
        request,
        target: app.target(),
        apply,
        allow_duplicate,
        project: app.project_override.clone(),
    }
}

fn handle_confirm_native_key(app: &mut App, key: KeyEvent) -> UiCommand {
    match key.code {
        KeyCode::Esc | KeyCode::Char('q') | KeyCode::Char('n') | KeyCode::Char('N') => {
            app.view = View::Target;
            UiCommand::None
        }
        KeyCode::Char('e') | KeyCode::Char('E') => {
            app.project_input = app.import_project().unwrap_or_default();
            app.editing_project = true;
            UiCommand::None
        }
        KeyCode::Char('y') | KeyCode::Char('Y') => native_import_command(app, true, false),
        KeyCode::Left => {
            app.confirm_index = app.confirm_index.saturating_sub(1);
            UiCommand::None
        }
        KeyCode::Right => {
            app.confirm_index = (app.confirm_index + 1).min(2);
            UiCommand::None
        }
        KeyCode::Enter => {
            if app.confirm_index == 2 {
                app.view = View::Target;
                return UiCommand::None;
            }
            native_import_command(app, app.confirm_index == 1, false)
        }
        _ => UiCommand::None,
    }
}

fn handle_project_edit_key(app: &mut App, key: KeyEvent) -> UiCommand {
    match key.code {
        KeyCode::Esc => {
            app.editing_project = false;
        }
        KeyCode::Enter => {
            let value = expand_home(app, app.project_input.trim());
            app.project_override = if value.is_empty() { None } else { Some(value) };
            app.editing_project = false;
        }
        KeyCode::Backspace => {
            app.project_input.pop();
        }
        KeyCode::Char(value)
            if !key
                .modifiers
                .intersects(KeyModifiers::CONTROL | KeyModifiers::ALT) =>
        {
            app.project_input.push(value);
        }
        _ => {}
    }
    UiCommand::None
}

fn handle_confirm_duplicate_key(app: &mut App, key: KeyEvent) -> UiCommand {
    match key.code {
        KeyCode::Esc | KeyCode::Char('q') | KeyCode::Char('n') | KeyCode::Char('N') => {
            let back = app.flow_back_view();
            show_result(
                app,
                text(app.language, "Duplicate Cancelled", "已取消重复导入"),
                app.duplicate_message.clone(),
                back,
            );
            UiCommand::None
        }
        KeyCode::Char('y') | KeyCode::Char('Y') => native_import_command(app, true, true),
        KeyCode::Left | KeyCode::Right => {
            app.confirm_index = 1 - app.confirm_index.min(1);
            UiCommand::None
        }
        KeyCode::Enter => {
            if app.confirm_index == 1 {
                let back = app.flow_back_view();
                show_result(
                    app,
                    text(app.language, "Duplicate Cancelled", "已取消重复导入"),
                    app.duplicate_message.clone(),
                    back,
                );
                return UiCommand::None;
            }
            native_import_command(app, true, true)
        }
        _ => UiCommand::None,
    }
}

fn handle_result_key(app: &mut App, key: KeyEvent) -> UiCommand {
    let max_scroll = app.result_text.lines().count().saturating_sub(1) as u16;
    match key.code {
        KeyCode::Esc | KeyCode::Char('q') => {
            app.view = app.result_back_view;
            UiCommand::None
        }
        KeyCode::Up => {
            app.result_scroll = app.result_scroll.saturating_sub(1);
            UiCommand::None
        }
        KeyCode::Down => {
            app.result_scroll = app.result_scroll.saturating_add(1).min(max_scroll);
            UiCommand::None
        }
        KeyCode::PageUp => {
            app.result_scroll = app.result_scroll.saturating_sub(10);
            UiCommand::None
        }
        KeyCode::PageDown | KeyCode::Char(' ') => {
            app.result_scroll = app.result_scroll.saturating_add(10).min(max_scroll);
            UiCommand::None
        }
        KeyCode::Home => {
            app.result_scroll = 0;
            UiCommand::None
        }
        KeyCode::End => {
            app.result_scroll = max_scroll;
            UiCommand::None
        }
        KeyCode::Char('c') | KeyCode::Char('C') => UiCommand::CopyText(app.result_text.clone()),
        _ => UiCommand::None,
    }
}

fn handle_bundles_key(app: &mut App, key: KeyEvent) -> UiCommand {
    let rows = app.bundles.len() + 1; // + "type a path" row
    match key.code {
        KeyCode::Esc | KeyCode::Char('q') => {
            app.view = View::Home;
            UiCommand::None
        }
        KeyCode::Up => {
            app.bundle_index = app.bundle_index.saturating_sub(1);
            UiCommand::None
        }
        KeyCode::Down => {
            app.bundle_index = (app.bundle_index + 1).min(rows - 1);
            UiCommand::None
        }
        KeyCode::Char('r') | KeyCode::Char('R') => UiCommand::LoadBundles,
        KeyCode::Char('/') => {
            app.bundle_input.clear();
            app.view = View::BundlePath;
            UiCommand::None
        }
        KeyCode::Enter => {
            if let Some(bundle) = app.bundles.get(app.bundle_index).cloned() {
                return open_bundle(app, bundle);
            }
            app.bundle_input.clear();
            app.view = View::BundlePath;
            UiCommand::None
        }
        _ => UiCommand::None,
    }
}

fn open_bundle(app: &mut App, bundle: BundleRow) -> UiCommand {
    app.reset_import_state();
    let path = bundle.path.clone();
    app.bundle_origin = Some(bundle);
    UiCommand::LoadPreview {
        request: ImportRequest::Bundle { path },
        purpose: PreviewPurpose::Import,
    }
}

fn handle_bundle_path_key(app: &mut App, key: KeyEvent) -> UiCommand {
    match key.code {
        KeyCode::Esc => {
            app.view = View::Bundles;
            UiCommand::None
        }
        KeyCode::Enter => {
            let path = expand_home(app, app.bundle_input.trim());
            if path.is_empty() {
                app.status_message = Some(text_for(
                    app.language,
                    "Type the path of a .json bundle first.",
                    "请先输入 .json 包的路径。",
                ));
                return UiCommand::None;
            }
            open_bundle(app, BundleRow::from_path(&path))
        }
        KeyCode::Backspace => {
            app.bundle_input.pop();
            UiCommand::None
        }
        KeyCode::Char(value)
            if !key
                .modifiers
                .intersects(KeyModifiers::CONTROL | KeyModifiers::ALT) =>
        {
            app.bundle_input.push(value);
            UiCommand::None
        }
        _ => UiCommand::None,
    }
}

/// "~/x" -> "<home>/x" so typed paths work the same as in a shell.
fn expand_home(app: &App, value: &str) -> String {
    if value == "~" {
        return app.home.to_string_lossy().to_string();
    }
    if let Some(rest) = value
        .strip_prefix("~/")
        .or_else(|| value.strip_prefix("~\\"))
    {
        return app.home.join(rest).to_string_lossy().to_string();
    }
    value.to_string()
}

fn handle_filter_key(app: &mut App, key: KeyEvent) -> UiCommand {
    match key.code {
        KeyCode::Esc => {
            app.filtering = false;
            UiCommand::None
        }
        KeyCode::Enter => {
            app.filter = app.filter_input.trim().to_string();
            app.filtering = false;
            app.selected_index = 0;
            UiCommand::None
        }
        KeyCode::Backspace => {
            app.filter_input.pop();
            UiCommand::None
        }
        KeyCode::Char(value) => {
            if key
                .modifiers
                .intersects(KeyModifiers::CONTROL | KeyModifiers::ALT)
            {
                return UiCommand::None;
            }
            app.filter_input.push(value);
            UiCommand::None
        }
        _ => UiCommand::None,
    }
}

fn handle_path_setup_key(app: &mut App, key: KeyEvent) -> UiCommand {
    match key.code {
        KeyCode::Esc => {
            app.view = View::Home;
            UiCommand::None
        }
        KeyCode::Up => {
            app.path_index = app.path_index.saturating_sub(1);
            UiCommand::None
        }
        KeyCode::Down => {
            app.path_index = (app.path_index + 1).min(PathTarget::all().len() - 1);
            UiCommand::None
        }
        KeyCode::Char('d') | KeyCode::Char('D')
            if key.modifiers.contains(KeyModifiers::CONTROL) =>
        {
            UiCommand::PathsDoctor
        }
        KeyCode::Enter => {
            let value = app.path_input.trim().to_string();
            if value.is_empty() {
                show_result(
                    app,
                    "Path Setup",
                    "Type a path before saving.",
                    View::PathSetup,
                );
                return UiCommand::None;
            }
            UiCommand::SetPath {
                target: app.path_target(),
                value,
            }
        }
        KeyCode::Backspace => {
            app.path_input.pop();
            UiCommand::None
        }
        KeyCode::Char(value) => {
            if key
                .modifiers
                .intersects(KeyModifiers::CONTROL | KeyModifiers::ALT)
            {
                return UiCommand::None;
            }
            app.path_input.push(value);
            UiCommand::None
        }
        _ => UiCommand::None,
    }
}

pub fn begin_command(app: &mut App, command: UiCommand) {
    let home = app.home.clone();
    let (tx, rx) = mpsc::channel();
    app.worker_rx = Some(rx);
    prepare_loading(app, &command);
    match command {
        UiCommand::LoadSessions {
            source,
            action: _,
            limit,
            preserve_filter: _,
        } => {
            thread::spawn(move || {
                let result = api_sessions(&home, source, limit);
                let _ = tx.send(WorkerResult::Sessions(result));
            });
        }
        UiCommand::BuildHandoff {
            source,
            target,
            session_id,
        } => {
            thread::spawn(move || {
                let result = api_handoff(&home, source, target, &session_id);
                let _ = tx.send(WorkerResult::Handoff(result));
            });
        }
        UiCommand::NativeImport {
            request,
            target,
            apply,
            allow_duplicate,
            project,
        } => {
            thread::spawn(move || {
                let result = api_native_import(
                    &home,
                    &request,
                    target,
                    apply,
                    allow_duplicate,
                    project.as_deref(),
                );
                let _ = tx.send(WorkerResult::Native(result));
            });
        }
        UiCommand::ExportSession { source, session_id } => {
            thread::spawn(move || {
                let result = api_export(&home, source, &session_id);
                let _ = tx.send(WorkerResult::Export(result));
            });
        }
        UiCommand::LoadBundles => {
            thread::spawn(move || {
                let _ = tx.send(WorkerResult::Bundles(api_bundles(&home)));
            });
        }
        UiCommand::LoadPreview { request, purpose } => {
            thread::spawn(move || {
                let result = api_preview(&home, &request).map(Box::new);
                let _ = tx.send(WorkerResult::Preview(purpose, result));
            });
        }
        UiCommand::PathsDoctor => {
            thread::spawn(move || {
                let result = api_paths(&home);
                let _ = tx.send(WorkerResult::Paths(result));
            });
        }
        UiCommand::SetPath { target, value } => {
            thread::spawn(move || {
                let result = api_set_path(&home, target, &value);
                let _ = tx.send(WorkerResult::PathSet(result));
            });
        }
        UiCommand::None | UiCommand::Quit | UiCommand::CopyText(_) => {
            app.worker_rx = None;
        }
    }
}

pub fn prepare_loading(app: &mut App, command: &UiCommand) {
    if app.view != View::Loading {
        app.loading_back_view = app.view;
    }
    let lang = app.language;
    let message = match command {
        UiCommand::LoadSessions {
            source,
            action,
            limit,
            preserve_filter,
        } => {
            app.pending_action = *action;
            app.session_limit = *limit;
            app.preserve_filter_after_load = *preserve_filter;
            match lang {
                Language::English => format!(
                    "Loading {} sessions with fast metadata path (limit {})...",
                    source.label(),
                    limit
                ),
                Language::Chinese => format!(
                    "正在加载 {} 会话（快速元数据，最多 {} 条）...",
                    source.label(),
                    limit
                ),
            }
        }
        UiCommand::BuildHandoff { target, .. } => match lang {
            Language::English => format!("Building handoff for {}...", target.label()),
            Language::Chinese => format!("正在为 {} 生成接续提示...", target.label()),
        },
        UiCommand::NativeImport { target, apply, .. } => match (lang, *apply) {
            (Language::English, true) => {
                format!("Writing native import into {}...", target.label())
            }
            (Language::English, false) => format!("Preparing dry-run for {}...", target.label()),
            (Language::Chinese, true) => format!("正在写入 {} ...", target.label()),
            (Language::Chinese, false) => format!("正在为 {} 生成预演...", target.label()),
        },
        UiCommand::ExportSession { source, .. } => match lang {
            Language::English => format!("Exporting {} session to a bundle...", source.label()),
            Language::Chinese => format!("正在把 {} 会话导出为 JSON 包...", source.label()),
        },
        UiCommand::LoadBundles => text_for(
            lang,
            "Looking for ChatBridge bundles...",
            "正在查找 ChatBridge JSON 包...",
        ),
        UiCommand::LoadPreview { .. } => text_for(
            lang,
            "Reading the session (large transcripts take a few seconds)...",
            "正在读取会话内容（大会话需要几秒）...",
        ),
        UiCommand::PathsDoctor => {
            text_for(lang, "Inspecting history paths...", "正在检查历史路径...")
        }
        UiCommand::SetPath { target, .. } => match lang {
            Language::English => format!("Saving {} override...", target.label_for(lang)),
            Language::Chinese => format!("正在保存 {} 路径...", target.label_for(lang)),
        },
        UiCommand::None | UiCommand::Quit | UiCommand::CopyText(_) => return,
    };
    app.loading_message = message;
    app.view = View::Loading;
}

fn drain_worker(app: &mut App) {
    let Some(rx) = app.worker_rx.take() else {
        return;
    };
    match rx.try_recv() {
        Ok(result) => apply_worker_result(app, result),
        Err(mpsc::TryRecvError::Empty) => {
            app.worker_rx = Some(rx);
        }
        Err(mpsc::TryRecvError::Disconnected) => {
            show_result(
                app,
                "Worker Error",
                "Background worker disconnected.",
                View::Home,
            );
        }
    }
}

pub fn apply_worker_result(app: &mut App, result: WorkerResult) {
    app.worker_rx = None;
    let lang = app.language;
    match result {
        WorkerResult::Sessions(Ok(data)) => {
            app.sessions = data.sessions;
            app.session_loaded = data.loaded;
            app.session_total = data.total;
            app.session_limit = data.limit;
            app.session_has_more = data.has_more;
            app.selected_index = 0;
            if !app.preserve_filter_after_load {
                app.filter.clear();
                app.filter_input.clear();
            }
            app.preserve_filter_after_load = false;
            app.view = View::Sessions;
        }
        WorkerResult::Handoff(Ok(text)) => {
            show_result(
                app,
                text_for(lang, "Prompt Handoff", "接续提示"),
                text,
                View::Sessions,
            );
        }
        WorkerResult::Native(Ok(text)) => {
            let back = app.flow_back_view();
            show_result(app, text_for(lang, "Native Import", "原生导入"), text, back);
        }
        WorkerResult::Export(Ok(text)) => {
            let hint = text_for(
                lang,
                "\n\nNext: copy this file to the other machine, run `chatbridge` there and press I (Import JSON Bundle). \
                 Or from a shell: chatbridge native-import --bundle <file> --to claude|codex|copilot --apply\n\
                 Press C to copy this text.",
                "\n\n下一步：把这个文件拷到另一台机器，在那里运行 `chatbridge` 按 I（导入 JSON 包）。\
                 或在命令行运行：chatbridge native-import --bundle <文件> --to claude|codex|copilot --apply\n\
                 按 C 复制本页文字。",
            );
            show_result(
                app,
                text_for(lang, "Export", "导出"),
                format!("{text}{hint}"),
                View::Sessions,
            );
        }
        WorkerResult::Bundles(Ok(data)) => {
            app.bundles = data.bundles;
            app.bundle_dirs = data.dirs;
            app.export_dir = data.export_dir;
            app.bundle_index = 0;
            app.bundle_origin = None;
            app.view = View::Bundles;
        }
        WorkerResult::Preview(PreviewPurpose::Import, Ok(data)) => {
            app.preview = Some(*data);
            app.preview_error = None;
            app.preview_confirm_index = 0;
            app.view = View::SessionPreview;
        }
        WorkerResult::Preview(PreviewPurpose::Import, Err(error)) => {
            if app.bundle_origin.is_some() {
                app.bundle_origin = None;
                show_result(
                    app,
                    text_for(lang, "Error", "错误"),
                    error.message,
                    View::Bundles,
                );
            } else {
                // The import itself may still work; let the user decide.
                app.preview = None;
                app.preview_error = Some(error.message);
                app.preview_confirm_index = 0;
                app.view = View::SessionPreview;
            }
        }
        WorkerResult::Preview(PreviewPurpose::Detail, result) => {
            let detail = match (&result, app.selected_session()) {
                (Ok(data), _) => preview_detail_text(data, lang),
                (Err(error), Some(session)) => format!(
                    "{}\n\n{}: {}",
                    session_detail(session),
                    text(lang, "Preview unavailable", "无法读取内容"),
                    error.message
                ),
                (Err(error), None) => error.message.clone(),
            };
            show_result(
                app,
                text_for(lang, "Session Detail", "会话详情"),
                detail,
                View::Sessions,
            );
        }
        WorkerResult::Status(Ok(data)) => {
            app.source_status = data.sources;
        }
        WorkerResult::Status(Err(_)) => {}
        WorkerResult::Paths(Ok(text)) => {
            show_result(
                app,
                text_for(lang, "Path Doctor", "路径诊断"),
                text,
                View::Home,
            );
        }
        WorkerResult::PathSet(Ok(text)) => {
            app.path_input.clear();
            show_result(
                app,
                text_for(lang, "Path Setup", "路径设置"),
                text,
                View::PathSetup,
            );
        }
        WorkerResult::Native(Err(error)) if error.kind == "duplicate" => {
            app.duplicate_message = error.message;
            app.duplicate_next_title = error.next_title.unwrap_or_else(|| "next copy".to_string());
            app.confirm_index = 0;
            app.view = View::ConfirmDuplicate;
        }
        WorkerResult::Sessions(Err(error)) | WorkerResult::Bundles(Err(error)) => {
            show_result(
                app,
                text_for(lang, "Error", "错误"),
                error.message,
                View::Home,
            );
        }
        WorkerResult::Handoff(Err(error))
        | WorkerResult::Native(Err(error))
        | WorkerResult::Export(Err(error)) => {
            let back = app.flow_back_view();
            show_result(app, text_for(lang, "Error", "错误"), error.message, back);
        }
        WorkerResult::Paths(Err(error)) => {
            show_result(
                app,
                text_for(lang, "Error", "错误"),
                error.message,
                View::Home,
            );
        }
        WorkerResult::PathSet(Err(error)) => {
            show_result(
                app,
                text_for(lang, "Error", "错误"),
                error.message,
                View::PathSetup,
            );
        }
    }
    app.clamp_selection();
}

/// Session detail (Recent action): metadata plus what the transcript contains.
fn preview_detail_text(data: &PreviewData, lang: Language) -> String {
    let mut lines = vec![
        format!("{}: {}", text(lang, "Title", "标题"), data.title),
        format!(
            "{}: {}  ({})",
            text(lang, "Source", "来源"),
            data.source_label,
            data.session_id
        ),
        format!(
            "{}: {}{}",
            text(lang, "Project", "项目"),
            data.project_path.as_deref().unwrap_or("-"),
            if data.project_exists || data.project_path.is_none() {
                ""
            } else {
                text(lang, "   [not found on this machine]", "   [本机不存在]")
            }
        ),
        format!(
            "{}: {}",
            text(lang, "Started", "开始"),
            format_time_label(&data.created_at)
        ),
        format!(
            "{}: {}",
            text(lang, "Updated", "更新"),
            format_time_label(&data.updated_at)
        ),
        String::new(),
        preview_counts_line(data, lang),
    ];
    if data.transcript_missing {
        lines.push(
            text(
                lang,
                "! Only the typed prompts were found (no transcript file); exports and imports contain just those.",
                "! 只找到提问记录，没有完整对话文件；导出/导入只会包含这些提问。",
            )
            .to_string(),
        );
    }
    if !data.files_touched.is_empty() {
        lines.push(String::new());
        lines.push(format!(
            "{} ({}):",
            text(lang, "Files touched", "改动过的文件"),
            data.files_touched.len()
        ));
        lines.extend(
            data.files_touched
                .iter()
                .take(15)
                .map(|path| format!("  - {path}")),
        );
    }
    for (label, value) in [
        (text(lang, "First prompt", "第一条提问"), &data.first_prompt),
        (text(lang, "Last prompt", "最后一条提问"), &data.last_prompt),
        (text(lang, "Last reply", "最后一条回复"), &data.last_reply),
    ] {
        if let Some(value) = value {
            lines.push(String::new());
            lines.push(format!("{label}:"));
            lines.push(format!("  {value}"));
        }
    }
    if !data.recent_tools.is_empty() {
        lines.push(String::new());
        lines.push(text(lang, "Recent tool calls:", "最近的工具调用：").to_string());
        lines.extend(data.recent_tools.iter().map(|item| format!("  - {item}")));
    }
    lines.join("\n")
}

fn preview_counts_line(data: &PreviewData, lang: Language) -> String {
    let breakdown = data.tool_breakdown(5);
    let tools = if breakdown.is_empty() {
        String::new()
    } else {
        format!(" ({breakdown})")
    };
    match lang {
        Language::English => format!(
            "{} user · {} assistant · {} tool calls{} · ~{} tokens",
            data.stat("user"),
            data.stat("assistant"),
            data.stat("toolCalls"),
            tools,
            format_count(data.stat("approxTokens"))
        ),
        Language::Chinese => format!(
            "用户 {} 条 · 助手 {} 条 · 工具调用 {} 次{} · 约 {} tokens",
            data.stat("user"),
            data.stat("assistant"),
            data.stat("toolCalls"),
            tools,
            format_count(data.stat("approxTokens"))
        ),
    }
}

fn format_count(value: u64) -> String {
    if value >= 1_000_000 {
        format!("{:.1}M", value as f64 / 1_000_000.0)
    } else if value >= 1_000 {
        format!("{:.1}K", value as f64 / 1_000.0)
    } else {
        value.to_string()
    }
}

fn api_sessions(home: &PathBuf, source: Source, limit: usize) -> ApiResult<SessionsData> {
    let limit_text = limit.to_string();
    let data: SessionsData = call_api(
        home,
        &[
            "api",
            "sessions",
            "--source",
            source.key(),
            "--limit",
            limit_text.as_str(),
        ],
    )?;
    Ok(data)
}

fn api_handoff(
    home: &PathBuf,
    source: Source,
    target: Target,
    session_id: &str,
) -> ApiResult<String> {
    let data: TextData = call_api(
        home,
        &[
            "api",
            "handoff",
            "--from",
            source.key(),
            "--to",
            target.key(),
            "--session",
            session_id,
            "--level",
            "normal",
        ],
    )?;
    Ok(data.text)
}

fn api_native_import(
    home: &PathBuf,
    request: &ImportRequest,
    target: Target,
    apply: bool,
    allow_duplicate: bool,
    project: Option<&str>,
) -> ApiResult<String> {
    let mut args: Vec<String> = vec!["api".into(), "native-import".into()];
    match request {
        ImportRequest::Session { source, session_id } => {
            args.extend([
                "--from".into(),
                source.key().into(),
                "--session".into(),
                session_id.clone(),
            ]);
        }
        ImportRequest::Bundle { path } => {
            args.extend(["--bundle".into(), path.clone()]);
        }
    }
    args.extend([
        "--to".into(),
        target.key().into(),
        "--level".into(),
        "full".into(),
        if apply { "--apply" } else { "--dry-run" }.into(),
    ]);
    if let Some(project) = project.filter(|value| !value.trim().is_empty()) {
        args.extend(["--project".into(), project.to_string()]);
    }
    if allow_duplicate {
        args.push("--allow-duplicate".into());
    }
    let refs: Vec<&str> = args.iter().map(String::as_str).collect();
    let data: TextData = call_api(home, &refs)?;
    Ok(data.text)
}

fn api_preview(home: &PathBuf, request: &ImportRequest) -> ApiResult<PreviewData> {
    match request {
        ImportRequest::Session { source, session_id } => call_api(
            home,
            &[
                "api",
                "preview",
                "--from",
                source.key(),
                "--session",
                session_id.as_str(),
            ],
        ),
        ImportRequest::Bundle { path } => {
            call_api(home, &["api", "preview", "--bundle", path.as_str()])
        }
    }
}

fn api_bundles(home: &PathBuf) -> ApiResult<BundlesData> {
    call_api(home, &["api", "bundles"])
}

fn api_status(home: &PathBuf) -> ApiResult<StatusData> {
    call_api(home, &["api", "paths", "status"])
}

fn api_paths(home: &PathBuf) -> ApiResult<String> {
    let data: TextData = call_api(home, &["api", "paths", "doctor"])?;
    Ok(data.text)
}

/// Bundles go to ~/.chatbridge/exports/ (not the launch directory, which is often a git repo).
fn export_dir_arg(home: &std::path::Path) -> String {
    let dir = home.join(".chatbridge").join("exports");
    format!("{}{}", dir.to_string_lossy(), std::path::MAIN_SEPARATOR)
}

fn api_export(home: &PathBuf, source: Source, session_id: &str) -> ApiResult<String> {
    let out = export_dir_arg(home);
    let data: TextData = call_api(
        home,
        &[
            "api",
            "export",
            "--from",
            source.key(),
            "--session",
            session_id,
            "--out",
            out.as_str(),
        ],
    )?;
    Ok(data.text)
}

fn api_set_path(home: &PathBuf, target: PathTarget, value: &str) -> ApiResult<String> {
    let data: TextData = call_api(home, &["api", "paths", "set", target.api_arg(), value])?;
    Ok(data.text)
}

fn api_timeout() -> Duration {
    let secs = env::var("CHATBRIDGE_API_TIMEOUT")
        .ok()
        .and_then(|value| value.trim().parse::<u64>().ok())
        .filter(|value| *value > 0)
        .unwrap_or(DEFAULT_API_TIMEOUT_SECS);
    Duration::from_secs(secs)
}

fn call_api<T: DeserializeOwned>(home: &PathBuf, args: &[&str]) -> ApiResult<T> {
    let python = env::var("PYTHON")
        .or_else(|_| env::var("PYTHON3"))
        .unwrap_or_else(|_| {
            if cfg!(windows) {
                "python".to_string()
            } else {
                "python3".to_string()
            }
        });
    let mut child = Command::new(python)
        .arg("-m")
        .arg("chatbridge")
        .arg("--home")
        .arg(home)
        .args(args)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|error| ApiError {
            kind: "error".to_string(),
            message: format!("Failed to start Python bridge: {error}"),
            next_title: None,
        })?;

    // Drain the pipes on helper threads so a chatty child can never deadlock us.
    let stdout_pipe = child.stdout.take();
    let stderr_pipe = child.stderr.take();
    let stdout_handle = thread::spawn(move || read_pipe(stdout_pipe));
    let stderr_handle = thread::spawn(move || read_pipe(stderr_pipe));

    let timeout = api_timeout();
    let deadline = Instant::now() + timeout;
    let status = loop {
        match child.try_wait() {
            Ok(Some(status)) => break status,
            Ok(None) => {
                if Instant::now() >= deadline {
                    let _ = child.kill();
                    let _ = child.wait();
                    let _ = stdout_handle.join();
                    let _ = stderr_handle.join();
                    return Err(ApiError {
                        kind: "timeout".to_string(),
                        message: format!(
                            "Python bridge timed out after {}s and was terminated. Set CHATBRIDGE_API_TIMEOUT to raise the limit.",
                            timeout.as_secs()
                        ),
                        next_title: None,
                    });
                }
                thread::sleep(Duration::from_millis(50));
            }
            Err(error) => {
                let _ = child.kill();
                return Err(ApiError {
                    kind: "error".to_string(),
                    message: format!("Failed to wait for Python bridge: {error}"),
                    next_title: None,
                });
            }
        }
    };
    let stdout = stdout_handle.join().unwrap_or_default();
    let stderr = stderr_handle.join().unwrap_or_default();

    if !status.success() {
        return Err(ApiError {
            kind: "error".to_string(),
            message: String::from_utf8_lossy(&stderr).trim().to_string(),
            next_title: None,
        });
    }
    let envelope: ApiEnvelope<T> = serde_json::from_slice(&stdout).map_err(|error| ApiError {
        kind: "error".to_string(),
        message: format!("Invalid JSON from Python bridge: {error}"),
        next_title: None,
    })?;
    if envelope.ok {
        envelope.data.ok_or_else(|| ApiError {
            kind: "error".to_string(),
            message: "Python bridge returned ok without data.".to_string(),
            next_title: None,
        })
    } else {
        Err(ApiError {
            kind: envelope.kind.unwrap_or_else(|| "error".to_string()),
            message: envelope
                .message
                .unwrap_or_else(|| "Unknown Python bridge error.".to_string()),
            next_title: envelope.next_title,
        })
    }
}

fn read_pipe<R: Read>(pipe: Option<R>) -> Vec<u8> {
    let mut buffer = Vec::new();
    if let Some(mut reader) = pipe {
        let _ = reader.read_to_end(&mut buffer);
    }
    buffer
}

pub fn render(frame: &mut Frame<'_>, app: &mut App) {
    let area = frame.area();
    frame.render_widget(Block::default(), area);
    let rows = Layout::default()
        .direction(Direction::Vertical)
        .constraints([
            Constraint::Length(3),
            Constraint::Min(8),
            Constraint::Length(2),
        ])
        .split(area);
    render_header(frame, app, rows[0]);
    render_body(frame, app, rows[1]);
    render_footer(frame, app, rows[2]);
    if app.view == View::Loading {
        render_loading(frame, app, area);
    }
    if app.view == View::SessionPreview {
        render_session_preview(frame, app, area);
    }
    if app.view == View::ConfirmDuplicate {
        render_duplicate_confirm(frame, app, area);
    }
    if app.filtering {
        render_filter(frame, app, centered_rect(58, 18, area));
    }
}

fn render_header(frame: &mut Frame<'_>, app: &App, area: Rect) {
    let header = Block::default()
        .borders(Borders::ALL)
        .border_type(BorderType::Plain)
        .border_style(border_style());
    let inner = header.inner(area);
    frame.render_widget(header, area);

    let chunks = Layout::default()
        .direction(Direction::Horizontal)
        .constraints([
            Constraint::Length(22),
            Constraint::Min(20),
            Constraint::Length(24),
        ])
        .split(inner);

    let tabs_line = Line::from(
        Source::all()
            .iter()
            .enumerate()
            .flat_map(|(index, source)| {
                let style = if index == app.source_index {
                    selected_style()
                } else {
                    inactive_chip_style()
                };
                let mut spans = Vec::with_capacity(2);
                if index > 0 {
                    spans.push(Span::raw(" "));
                }
                spans.push(Span::styled(format!(" {} ", source.short_label()), style));
                spans
            })
            .collect::<Vec<_>>(),
    );

    frame.render_widget(
        Paragraph::new(Line::from(vec![
            Span::styled(" ChatBridge", accent_style()),
            Span::styled(format!(" v{VERSION}"), subtle_style()),
        ]))
        .alignment(Alignment::Left)
        .style(text_style()),
        chunks[0],
    );
    frame.render_widget(
        Paragraph::new(tabs_line)
            .alignment(Alignment::Center)
            .style(text_style()),
        chunks[1],
    );
    frame.render_widget(
        Paragraph::new(Line::from(Span::styled(
            format!(" Source: {} ", app.source().short_label()),
            selected_style(),
        )))
        .alignment(Alignment::Right)
        .style(text_style()),
        chunks[2],
    );
}

/// Below this width the Actions panel is only shown on the dashboard, so lists get the room.
const NARROW_WIDTH: u16 = 100;

fn render_body(frame: &mut Frame<'_>, app: &mut App, area: Rect) {
    if area.width < NARROW_WIDTH && app.view != View::Home {
        render_right(frame, app, area);
        return;
    }
    let chunks = Layout::default()
        .direction(Direction::Horizontal)
        .constraints([
            Constraint::Length(34),
            Constraint::Length(1),
            Constraint::Min(40),
        ])
        .split(area);
    render_left(frame, app, chunks[0]);
    render_right(frame, app, chunks[2]);
}

fn render_left(frame: &mut Frame<'_>, app: &App, area: Rect) {
    let items = ChatAction::all()
        .iter()
        .map(|action| {
            ListItem::new(Line::from(vec![
                Span::raw(" "),
                Span::styled(format!(" {} ", action.hotkey()), inactive_chip_style()),
                Span::raw("  "),
                Span::styled(action.label_for(app.language), text_style()),
            ]))
        })
        .collect::<Vec<_>>();
    let mut state = ListState::default();
    state.select(Some(app.action_index));
    let list = List::new(items)
        .block(panel(text(app.language, " Actions ", " 操作 ")))
        .highlight_style(selected_style())
        .highlight_symbol(" ");
    frame.render_stateful_widget(list, area, &mut state);
}

fn render_right(frame: &mut Frame<'_>, app: &mut App, area: Rect) {
    let flow_view = |app: &App| -> View {
        if app.bundle_origin.is_some() {
            View::Bundles
        } else {
            View::Sessions
        }
    };
    let background = match app.view {
        // Modals draw over the view the user came from, not always Home.
        View::SessionPreview | View::ConfirmDuplicate => flow_view(app),
        View::Loading => match app.loading_back_view {
            View::Sessions
            | View::SessionPreview
            | View::Target
            | View::ConfirmNative
            | View::ConfirmDuplicate => flow_view(app),
            View::Bundles | View::BundlePath => View::Bundles,
            View::PathSetup => View::PathSetup,
            View::Result => app.result_back_view,
            _ => View::Home,
        },
        other => other,
    };
    match background {
        View::Sessions => render_sessions(frame, app, area),
        View::Target => render_target(frame, app, area),
        View::ConfirmNative => render_confirm_native(frame, app, area),
        View::PathSetup => render_path_setup(frame, app, area),
        View::Result => render_result(frame, app, area),
        View::Bundles => render_bundles(frame, app, area),
        View::BundlePath => render_bundle_path(frame, app, area),
        _ => render_home(frame, app, area),
    }
}

fn source_status_spans(app: &App) -> Vec<Span<'static>> {
    if app.source_status.is_empty() {
        let label = if app.status_rx.is_some() {
            text(app.language, "checking...", "检测中...")
        } else {
            "-"
        };
        return vec![Span::styled(label.to_string(), subtle_style())];
    }
    let mut spans = Vec::new();
    for source in Source::all() {
        let Some(status) = app
            .source_status
            .iter()
            .find(|status| status.source == source.key())
        else {
            continue;
        };
        if !spans.is_empty() {
            spans.push(Span::raw("  "));
        }
        let (mark, style) = if status.exists {
            ("✓", success_style())
        } else {
            ("✗", danger_style())
        };
        spans.push(Span::styled(
            format!("{} {mark}", source.short_label()),
            style,
        ));
    }
    spans
}

fn render_home(frame: &mut Frame<'_>, app: &App, area: Rect) {
    let action = app.action();
    let compact = area.width < 60;
    let lang = app.language;
    let mut lines = if compact {
        vec![
            Line::from(Span::styled("  ChatBridge", accent_style())),
            Line::from(Span::styled("  AI history bridge", subtle_style())),
            Line::from(""),
            Line::from(vec![
                Span::styled("  Source ", label_style()),
                Span::styled(format!(" {} ", app.source().short_label()), value_style()),
                Span::raw("  "),
                Span::styled(" ready ", success_style()),
            ]),
            Line::from(vec![
                Span::styled("  Action ", label_style()),
                Span::styled(action.label_for(lang), value_style()),
            ]),
        ]
    } else {
        let mut sources = vec![Span::styled(
            format!("  {}", pad_to(text(lang, "History", "历史目录"), 12)),
            label_style(),
        )];
        sources.push(Span::raw(" "));
        sources.extend(source_status_spans(app));
        vec![
            Line::from(vec![
                Span::styled("  ChatBridge Interactive Mode", accent_style()),
                Span::styled("  AI history bridge", subtle_style()),
            ]),
            Line::from(""),
            Line::from(vec![Span::styled(
                format!("  {}", text(lang, "Connection Details", "连接信息")),
                label_style(),
            )]),
            Line::from(vec![
                Span::styled(
                    format!("  {}", pad_to(text(lang, "Source", "来源"), 12)),
                    label_style(),
                ),
                Span::styled(format!(" {} ", app.source().label()), value_style()),
                Span::raw("   "),
                Span::styled(format!("{}  ", text(lang, "Status", "状态")), label_style()),
                Span::styled(" ready ", success_style()),
            ]),
            Line::from(sources),
            Line::from(vec![
                Span::styled(
                    format!("  {}", pad_to(text(lang, "Action", "操作"), 12)),
                    label_style(),
                ),
                Span::styled(format!(" {}", action.label_for(lang)), value_style()),
            ]),
            Line::from(vec![
                Span::styled(
                    format!("  {}", pad_to(text(lang, "Workflow", "说明"), 12)),
                    label_style(),
                ),
                Span::styled(format!(" {}", action.description_for(lang)), subtle_style()),
            ]),
        ]
    };
    lines.push(Line::from(""));
    lines.extend(chat_bridge_logo_lines(area.width));
    frame.render_widget(
        content_block(text(lang, "Dashboard", "控制台"), lines),
        area,
    );
}

fn render_sessions(frame: &mut Frame<'_>, app: &mut App, area: Rect) {
    let chunks = Layout::default()
        .direction(Direction::Vertical)
        .constraints([Constraint::Length(2), Constraint::Min(4)])
        .split(area);
    let indices = app.filtered_indices();
    let mut rows = indices
        .iter()
        .enumerate()
        .map(|(visible_index, session_index)| {
            let session = &app.sessions[*session_index];
            ListItem::new(session_list_lines(
                session,
                visible_index,
                chunks[1].width,
                app.language,
            ))
        })
        .collect::<Vec<_>>();
    if app.session_has_more {
        rows.push(ListItem::new(Line::from(vec![
            Span::styled(" +  ", secondary_style()),
            Span::styled(
                text(app.language, "Load more...", "加载更多..."),
                accent_style(),
            ),
            Span::styled(
                format!("  Loaded {} / {}", app.session_loaded, app.session_total),
                muted_style(),
            ),
        ])));
    }
    let mut state = ListState::default();
    let selectable = rows.len();
    if selectable > 0 {
        state.select(Some(app.selected_index.min(selectable - 1)));
    }
    let filter_text = if app.filter.is_empty() {
        String::new()
    } else {
        format!(" | filter: {}", app.filter)
    };
    let header = match app.language {
        Language::English => format!(
            "{}  Loaded {} / {}  Limit {}{}",
            app.pending_action.label_for(app.language),
            app.session_loaded,
            app.session_total,
            app.session_limit,
            filter_text
        ),
        Language::Chinese => format!(
            "{}  已加载 {} / 共 {}  上限 {}{}",
            app.pending_action.label_for(app.language),
            app.session_loaded,
            app.session_total,
            app.session_limit,
            filter_text.replace(" | filter: ", " | 筛选: ")
        ),
    };
    frame.render_widget(Paragraph::new(header).style(subtle_style()), chunks[0]);
    let list = List::new(rows)
        .block(active_panel(format!(
            " {} ",
            app.pending_action.label_for(app.language)
        )))
        .style(text_style())
        .highlight_style(selected_style())
        .highlight_symbol(" ");
    frame.render_stateful_widget(list, chunks[1], &mut state);
}

fn origin_label(app: &App) -> &'static str {
    if app.bundle_origin.is_some() {
        text(app.language, "Bundle", "JSON 包")
    } else {
        text(app.language, "Session", "会话")
    }
}

fn target_description(target: Target, language: Language) -> &'static str {
    match target {
        Target::Codex => text(
            language,
            "Writes a Codex thread for the provider in ~/.codex/config.toml; it shows up in `codex resume`.",
            "写入 Codex 会话（使用 ~/.codex/config.toml 里的 provider），可在 `codex resume` 里看到。",
        ),
        Target::Claude => text(
            language,
            "Writes a Claude Code session into the project's history; open it with `claude --resume`.",
            "写入 Claude Code 项目历史，用 `claude --resume` 打开。",
        ),
        Target::Copilot => text(
            language,
            "Writes a VS Code Copilot chat. Quit every VS Code window first.",
            "写入 VS Code Copilot 聊天记录。写入前请完全退出所有 VS Code 窗口。",
        ),
        Target::Export => text(
            language,
            "Saves a portable JSON bundle (with tool calls and diffs) to ~/.chatbridge/exports/.",
            "导出可携带的 JSON 包（含工具调用和代码改动）到 ~/.chatbridge/exports/。",
        ),
    }
}

fn render_target(frame: &mut Frame<'_>, app: &App, area: Rect) {
    let lang = app.language;
    let options = app
        .targets()
        .iter()
        .enumerate()
        .map(|(index, target)| {
            let style = if index == app.target_index % app.targets().len() {
                selected_style()
            } else {
                inactive_chip_style()
            };
            Span::styled(format!(" {} ", target.label()), style)
        })
        .flat_map(|chip| [chip, Span::raw(" ")])
        .collect::<Vec<_>>();
    let heading = if app.pending_action == ChatAction::Handoff && app.bundle_origin.is_none() {
        text(
            lang,
            "Generate a handoff prompt for",
            "为哪个工具生成接续提示",
        )
    } else {
        text(lang, "Choose a target", "选择目标")
    };
    let lines = vec![
        Line::from(Span::styled(heading, accent_style())),
        Line::from(""),
        Line::from(vec![
            Span::styled(format!("{}: ", origin_label(app)), label_style()),
            Span::raw(app.import_title()),
        ]),
        Line::from(""),
        Line::from(options),
        Line::from(""),
        Line::from(Span::styled(
            target_description(app.target(), lang),
            subtle_style(),
        )),
    ];
    frame.render_widget(content_block(text(lang, " Target ", " 目标 "), lines), area);
}

fn button_row(options: &[&str], selected: usize) -> Line<'static> {
    let mut spans = vec![Span::raw("  ")];
    for (index, label) in options.iter().enumerate() {
        if index > 0 {
            spans.push(Span::raw("  "));
        }
        let style = if index == selected {
            selected_style()
        } else {
            inactive_chip_style()
        };
        spans.push(Span::styled(format!("[ {label} ]"), style));
    }
    Line::from(spans)
}

fn render_confirm_native(frame: &mut Frame<'_>, app: &App, area: Rect) {
    let lang = app.language;
    let options = [
        text(lang, "Dry run", "预演"),
        text(lang, "Apply", "执行"),
        text(lang, "Cancel", "取消"),
    ];
    let mut lines = vec![
        Line::from(Span::styled(
            text(lang, "Native Import", "原生导入"),
            accent_style(),
        )),
        Line::from(""),
        Line::from(vec![
            Span::styled(format!("{}: ", origin_label(app)), label_style()),
            Span::raw(app.import_title()),
        ]),
    ];
    if let Some(session) = app
        .selected_session()
        .filter(|_| app.bundle_origin.is_none())
    {
        lines.push(Line::from(format!(
            "{}: {}   {}: {}",
            text(lang, "Started", "开始"),
            session.start_time_label(),
            text(lang, "Updated", "更新"),
            session.updated_time_label()
        )));
    }
    lines.push(Line::from(vec![
        Span::styled(format!("{}: ", text(lang, "Target", "目标")), label_style()),
        Span::raw(app.target().label()),
    ]));
    let project = app.import_project();
    let project_label = project
        .clone()
        .unwrap_or_else(|| text(lang, "(current directory)", "（当前目录）").to_string());
    if app.editing_project {
        lines.push(Line::from(Span::styled(
            text(
                lang,
                "Import into project (type a path, Enter saves, Esc cancels):",
                "导入到项目（输入路径，Enter 保存，Esc 取消）：",
            ),
            label_style(),
        )));
        lines.push(Line::from(vec![
            Span::raw("  "),
            Span::styled(format!(" {}▏", app.project_input), selected_style()),
        ]));
    } else {
        lines.push(Line::from(vec![
            Span::raw(format!(
                "{}: ",
                text(lang, "Import into project", "导入到项目")
            )),
            Span::styled(project_label, secondary_style()),
            Span::styled(
                format!("   [E {}]", text(lang, "change", "修改")),
                subtle_style(),
            ),
        ]));
        if let Some(path) = project.as_deref() {
            if !path.contains("://") && !std::path::Path::new(path).is_dir() {
                lines.push(Line::from(Span::styled(
                    text(
                        lang,
                        "  ! This folder does not exist on this machine. Press E to import into a local project.",
                        "  ! 本机没有这个目录。按 E 改成本机的项目路径。",
                    ),
                    danger_style(),
                )));
            }
        }
    }
    if let Some(estimate) = app
        .preview
        .as_ref()
        .and_then(|preview| preview.import_estimate.as_ref())
    {
        let size = match lang {
            Language::English => format!(
                "Import size: ~{} tokens in {} messages",
                format_count(estimate.approx_tokens),
                estimate.messages
            ),
            Language::Chinese => format!(
                "导入大小：约 {} tokens，{} 条消息",
                format_count(estimate.approx_tokens),
                estimate.messages
            ),
        };
        lines.push(Line::from(size));
        if estimate.trimmed {
            lines.push(Line::from(Span::styled(
                format!(
                    "  {}: {}",
                    text(
                        lang,
                        "Trimmed to fit the target's context",
                        "为适配目标上下文已裁剪"
                    ),
                    estimate.notes.join("; ")
                ),
                subtle_style(),
            )));
        }
    }
    lines.extend([
        Line::from(""),
        button_row(&options, app.confirm_index),
        Line::from(""),
        Line::from(Span::styled(
            text(
                lang,
                "Enter confirm  Y apply now  E change project  N/Esc cancel",
                "Enter 确认  Y 直接执行  E 修改项目  N/Esc 取消",
            ),
            subtle_style(),
        )),
    ]);
    frame.render_widget(
        content_block(text(lang, " Confirm ", " 确认 "), lines),
        area,
    );
}

fn render_path_setup(frame: &mut Frame<'_>, app: &App, area: Rect) {
    let mut lines = vec![
        Line::from(Span::styled(
            text(app.language, "Path Setup", "路径设置"),
            accent_style(),
        )),
        Line::from(""),
    ];
    for (index, target) in PathTarget::all().iter().enumerate() {
        let style = if index == app.path_index {
            selected_style()
        } else {
            inactive_chip_style()
        };
        lines.push(Line::from(vec![
            Span::raw("  "),
            Span::styled(format!(" {} ", target.label_for(app.language)), style),
            Span::raw("  "),
            Span::styled(target.hint_for(app.language), subtle_style()),
        ]));
    }
    let input = if app.path_input.is_empty() {
        text(app.language, "<type path here>", "<在这里输入路径>").to_string()
    } else {
        app.path_input.clone()
    };
    lines.extend([
        Line::from(""),
        Line::from(Span::styled(
            text(app.language, "Input", "输入"),
            label_style(),
        )),
        Line::from(vec![
            Span::raw("  "),
            Span::styled(format!(" {input} "), selected_style()),
        ]),
        Line::from(""),
        Line::from(Span::styled(
            text(
                app.language,
                "Saved overrides are written to ~/.chatbridge/config.json.",
                "保存后的覆盖路径会写入 ~/.chatbridge/config.json。",
            ),
            subtle_style(),
        )),
    ]);
    frame.render_widget(
        content_block(text(app.language, " Paths ", " 路径 "), lines),
        area,
    );
}

fn render_session_preview(frame: &mut Frame<'_>, app: &App, frame_area: Rect) {
    let lang = app.language;
    let modal_width = frame_area.width.saturating_mul(80) / 100;
    let width = usize::from(modal_width.saturating_sub(16)).max(20);
    let options = [text(lang, "Continue", "继续"), text(lang, "Cancel", "取消")];
    let field = |label: &str, value: String| -> Line<'static> {
        Line::from(vec![
            Span::styled(format!("{label}: "), label_style()),
            Span::raw(compact_title(&value, width)),
        ])
    };
    let mut lines = vec![field(text(lang, "Title", "标题"), app.import_title())];
    if let Some(bundle) = &app.bundle_origin {
        lines.push(field(
            text(lang, "Bundle", "JSON 包"),
            tilde(app, &bundle.path),
        ));
    }
    let session = app
        .selected_session()
        .filter(|_| app.bundle_origin.is_none());
    match &app.preview {
        Some(preview) => {
            if preview.transcript_missing {
                lines.push(Line::from(Span::styled(
                    compact_title(
                        text(
                            lang,
                            "! Only the typed prompts were found (no transcript file); the import will contain just those.",
                            "! 只找到提问记录，没有完整对话文件；导入/导出只会包含这些提问。",
                        ),
                        width + 10,
                    ),
                    danger_style(),
                )));
            }
            lines.push(field(
                text(lang, "Source", "来源"),
                format!("{}  ({})", preview.source_label, preview.session_id),
            ));
            let project = preview
                .project_path
                .clone()
                .unwrap_or_else(|| "-".to_string());
            let mut project_line = vec![
                Span::styled(
                    format!("{}: ", text(lang, "Project", "项目")),
                    label_style(),
                ),
                Span::raw(compact_title(
                    &tilde(app, &project),
                    width.saturating_sub(24),
                )),
            ];
            if preview.project_path.is_some() {
                project_line.push(if preview.project_exists {
                    Span::styled(
                        format!("  ✓ {}", text(lang, "exists here", "本机存在")),
                        success_style(),
                    )
                } else {
                    Span::styled(
                        format!("  ✗ {}", text(lang, "not on this machine", "本机不存在")),
                        danger_style(),
                    )
                });
            }
            lines.push(Line::from(project_line));
            lines.push(field(
                text(lang, "Time", "时间"),
                format!(
                    "{} → {}",
                    format_time_label(&preview.created_at),
                    format_time_label(&preview.updated_at)
                ),
            ));
            lines.push(Line::from(""));
            lines.push(Line::from(Span::styled(
                compact_title(&preview_counts_line(preview, lang), width + 10),
                value_style(),
            )));
            if !preview.files_touched.is_empty() {
                let shown = preview
                    .files_touched
                    .iter()
                    .take(3)
                    .map(|path| tilde(app, path))
                    .collect::<Vec<_>>()
                    .join(", ");
                let more = preview.files_touched.len().saturating_sub(3);
                let suffix = if more > 0 {
                    format!(" (+{more})")
                } else {
                    String::new()
                };
                lines.push(field(
                    text(lang, "Files", "改动文件"),
                    format!("{shown}{suffix}"),
                ));
            }
            if let Some(prompt) = &preview.first_prompt {
                lines.push(field(
                    text(lang, "First prompt", "第一条提问"),
                    prompt.clone(),
                ));
            }
            if let Some(reply) = &preview.last_reply {
                lines.push(field(text(lang, "Last reply", "最后回复"), reply.clone()));
            }
            if let Some(estimate) = &preview.import_estimate {
                let summary = match lang {
                    Language::English => format!(
                        "~{} tokens{}",
                        format_count(estimate.approx_tokens),
                        if estimate.trimmed {
                            " (older tool output trimmed to fit)"
                        } else {
                            ""
                        }
                    ),
                    Language::Chinese => format!(
                        "约 {} tokens{}",
                        format_count(estimate.approx_tokens),
                        if estimate.trimmed {
                            "（较早的工具输出已裁剪）"
                        } else {
                            ""
                        }
                    ),
                };
                lines.push(field(text(lang, "Import size", "导入大小"), summary));
            }
        }
        None => {
            if let Some(session) = session {
                lines.push(field(
                    text(lang, "Project", "项目"),
                    session.project_label(),
                ));
                lines.push(field(
                    text(lang, "Started", "开始"),
                    session.start_time_label(),
                ));
                lines.push(field(
                    text(lang, "Updated", "更新"),
                    session.updated_time_label(),
                ));
                lines.push(field(
                    text(lang, "Source", "来源"),
                    format!(
                        "{}  {}: {}",
                        session.source_label,
                        text(lang, "Scope", "范围"),
                        session.scope_label()
                    ),
                ));
                lines.push(field("Session ID", session.session_id.clone()));
            }
            if let Some(error) = &app.preview_error {
                lines.push(Line::from(""));
                lines.push(Line::from(Span::styled(
                    compact_title(
                        &format!(
                            "{}: {error}",
                            text(lang, "Preview unavailable", "无法读取内容")
                        ),
                        width + 10,
                    ),
                    danger_style(),
                )));
            }
        }
    }
    lines.extend([
        Line::from(""),
        button_row(&options, app.preview_confirm_index),
        Line::from(""),
        Line::from(Span::styled(
            text(
                lang,
                "Enter confirm  Y continue  N/Esc cancel",
                "Enter 确认  Y 继续  N/Esc 取消",
            ),
            subtle_style(),
        )),
    ]);
    let area = centered_rect_lines(80, lines.len() + 1, frame_area);
    frame.render_widget(Clear, area);
    frame.render_widget(
        content_block(text(lang, " Chat Preview ", " 会话预览 "), lines),
        area,
    );
}

fn render_bundles(frame: &mut Frame<'_>, app: &App, area: Rect) {
    let lang = app.language;
    let chunks = Layout::default()
        .direction(Direction::Vertical)
        .constraints([Constraint::Length(2), Constraint::Min(4)])
        .split(area);
    let width = usize::from(chunks[1].width).saturating_sub(10).max(20);
    let mut rows = app
        .bundles
        .iter()
        .enumerate()
        .map(|(index, bundle)| {
            let stats = bundle
                .stats
                .as_ref()
                .map(|stats| match lang {
                    Language::English => format!(
                        " · {} msgs · {} tool calls",
                        stats.messages.unwrap_or(0),
                        stats.tool_calls.unwrap_or(0)
                    ),
                    Language::Chinese => format!(
                        " · {} 条消息 · {} 次工具调用",
                        stats.messages.unwrap_or(0),
                        stats.tool_calls.unwrap_or(0)
                    ),
                })
                .unwrap_or_default();
            let exported = if bundle.exported_at.is_some() {
                format_time_label(&bundle.exported_at)
            } else {
                format_time_label(&bundle.modified)
            };
            ListItem::new(vec![
                Line::from(vec![
                    Span::styled(format!("{:>2}. ", index + 1), muted_style()),
                    Span::raw(compact_title(
                        &bundle.title_label(),
                        width.saturating_sub(10),
                    )),
                    Span::raw("  "),
                    Span::styled(bundle.source_label(), secondary_style()),
                ]),
                Line::from(vec![
                    Span::raw("    "),
                    Span::styled(
                        compact_title(
                            &format!(
                                "{} · {} · {}{}",
                                bundle.name,
                                format_size(bundle.size),
                                exported,
                                stats
                            ),
                            width,
                        ),
                        subtle_style(),
                    ),
                ]),
            ])
        })
        .collect::<Vec<_>>();
    rows.push(ListItem::new(Line::from(vec![
        Span::styled(" +  ", secondary_style()),
        Span::styled(
            text(
                lang,
                "Type a bundle path... (/)",
                "手动输入 JSON 包路径...（/）",
            ),
            accent_style(),
        ),
    ])));
    let mut state = ListState::default();
    state.select(Some(app.bundle_index.min(rows.len() - 1)));
    let header = if app.bundles.is_empty() {
        text_for(
            lang,
            "No bundles found yet. Export one with N -> Export bundle (.json), copy it here, or type a path below.",
            "还没找到 JSON 包。可以在 N 里选「Export bundle (.json)」导出，拷到这台机器，或在下面手动输入路径。",
        )
    } else {
        let dirs = app
            .bundle_dirs
            .iter()
            .map(|dir| tilde(app, dir))
            .collect::<Vec<_>>()
            .join(", ");
        match lang {
            Language::English => format!("{} bundles · searched {}", app.bundles.len(), dirs),
            Language::Chinese => format!("找到 {} 个 · 已搜索 {}", app.bundles.len(), dirs),
        }
    };
    frame.render_widget(
        Paragraph::new(header)
            .style(subtle_style())
            .wrap(Wrap { trim: true }),
        chunks[0],
    );
    let list = List::new(rows)
        .block(active_panel(format!(
            " {} ",
            ChatAction::ImportBundle.label_for(lang)
        )))
        .style(text_style())
        .highlight_style(selected_style())
        .highlight_symbol(" ");
    frame.render_stateful_widget(list, chunks[1], &mut state);
}

fn render_bundle_path(frame: &mut Frame<'_>, app: &App, area: Rect) {
    let lang = app.language;
    let input = if app.bundle_input.is_empty() {
        text(
            lang,
            "<~/Downloads/chatbridge-export-....json>",
            "<~/Downloads/chatbridge-export-....json>",
        )
        .to_string()
    } else {
        app.bundle_input.clone()
    };
    let mut lines =
        vec![
        Line::from(Span::styled(text(lang, "Bundle path", "JSON 包路径"), accent_style())),
        Line::from(""),
        Line::from(text(
            lang,
            "Type the path of a ChatBridge .json bundle (~ works), then press Enter to preview it.",
            "输入 ChatBridge .json 包的路径（支持 ~），按 Enter 预览。",
        )),
        Line::from(""),
        Line::from(vec![Span::raw("  "), Span::styled(format!(" {input}▏"), selected_style())]),
    ];
    if !app.export_dir.is_empty() {
        lines.push(Line::from(""));
        lines.push(Line::from(Span::styled(
            match lang {
                Language::English => format!(
                    "Tip: bundles copied into {} show up in the list automatically.",
                    app.export_dir
                ),
                Language::Chinese => format!(
                    "提示：放进 {} 的 JSON 包会自动出现在列表里。",
                    app.export_dir
                ),
            },
            subtle_style(),
        )));
    }
    frame.render_widget(
        content_block(text(lang, " Import JSON Bundle ", " 导入 JSON 包 "), lines),
        area,
    );
}

fn format_size(bytes: u64) -> String {
    if bytes >= 1024 * 1024 {
        format!("{:.1} MB", bytes as f64 / (1024.0 * 1024.0))
    } else if bytes >= 1024 {
        format!("{:.1} KB", bytes as f64 / 1024.0)
    } else {
        format!("{bytes} B")
    }
}

fn render_result(frame: &mut Frame<'_>, app: &App, area: Rect) {
    let title = format!(" {} ", app.result_title);
    let paragraph = Paragraph::new(app.result_text.clone())
        .block(active_panel(title))
        .style(text_style())
        .wrap(Wrap { trim: false })
        .scroll((app.result_scroll, 0));
    frame.render_widget(paragraph, area);
}

fn render_footer(frame: &mut Frame<'_>, app: &App, area: Rect) {
    let footer = if app.editing_project {
        text(
            app.language,
            "EDIT  Type project path  Enter save  Esc cancel",
            "编辑  输入项目路径  Enter 保存  Esc 取消",
        )
    } else {
        match app.view {
            View::Home => text(
                app.language,
                "NAV  Tab source  Up/Down action  Enter select  T language    ACT  L recent  H handoff  N import  I bundle  P paths  q quit",
                "导航  Tab 来源  上/下 操作  Enter 选择  T 语言    操作  L 最近  H 接续  N 导入  I 导入包  P 路径  q 退出",
            ),
            View::Sessions => text(
                app.language,
                "NAV  Up/Down session  Enter choose/load more  / search title/path/id  T language  Esc back",
                "导航  上/下 会话  Enter 选择/加载更多  / 搜标题/路径/id  T 语言  Esc 返回",
            ),
            View::SessionPreview => text(
                app.language,
                "NAV  Left/Right choice  Enter confirm  Y continue  N/Esc cancel  T language",
                "导航  左/右 选择  Enter 确认  Y 继续  N/Esc 取消  T 语言",
            ),
            View::Target => text(
                app.language,
                "NAV  Left/Right target  Enter continue  T language  Esc back",
                "导航  左/右 目标  Enter 继续  T 语言  Esc 返回",
            ),
            View::ConfirmNative => text(
                app.language,
                "NAV  Left/Right choice  Enter confirm  Y apply  E project  N/Esc cancel  T language",
                "导航  左/右 选择  Enter 确认  Y 执行  E 项目  N/Esc 取消  T 语言",
            ),
            View::ConfirmDuplicate => text(
                app.language,
                "NAV  Left/Right choice  Enter confirm  Y import  N/Esc cancel  T language",
                "导航  左/右 选择  Enter 确认  Y 导入  N/Esc 取消  T 语言",
            ),
            View::PathSetup => text(
                app.language,
                "NAV  Up/Down target  Type path  Enter save  Ctrl+D doctor  Esc back",
                "导航  上/下 目标  输入路径  Enter 保存  Ctrl+D 诊断  Esc 返回",
            ),
            View::Result => text(
                app.language,
                "NAV  Up/Down scroll  PgUp/PgDn page  C copy  Esc/q back  T language",
                "导航  上/下 滚动  PgUp/PgDn 翻页  C 复制  Esc/q 返回  T 语言",
            ),
            View::Loading => text(
                app.language,
                "Loading...  Esc cancel  Ctrl+C quit",
                "加载中...  Esc 取消  Ctrl+C 退出",
            ),
            View::Bundles => text(
                app.language,
                "NAV  Up/Down bundle  Enter preview  / type path  R rescan  T language  Esc back",
                "导航  上/下 选择  Enter 预览  / 输入路径  R 重新扫描  T 语言  Esc 返回",
            ),
            View::BundlePath => text(
                app.language,
                "Type path  Enter preview  Esc back",
                "输入路径  Enter 预览  Esc 返回",
            ),
        }
    };
    let line = match &app.status_message {
        Some(message) => Line::from(vec![
            Span::styled(message.clone(), secondary_style()),
            Span::styled(
                format!("    LANG {}    v{VERSION}", app.language.label()),
                subtle_style(),
            ),
        ]),
        None => Line::from(Span::styled(
            format!("{footer}    LANG {}    v{VERSION}", app.language.label()),
            subtle_style(),
        )),
    };
    frame.render_widget(
        Paragraph::new(line).block(
            Block::default()
                .borders(Borders::TOP)
                .border_style(border_style()),
        ),
        area,
    );
}

fn render_loading(frame: &mut Frame<'_>, app: &App, frame_area: Rect) {
    let frames = ["-", "\\", "|", "/"];
    let spinner = frames[app.tick % frames.len()];
    let modal_width = frame_area.width.saturating_mul(58) / 100;
    let bar_width = usize::from(modal_width.saturating_sub(10)).clamp(12, 34);
    let progress = loading_progress_bar(app.tick, bar_width);
    let lines = vec![
        Line::from(Span::styled(
            format!("{spinner} {}", text(app.language, "Working", "处理中")),
            accent_style(),
        )),
        Line::from(""),
        Line::from(app.loading_message.clone()),
        Line::from(""),
        Line::from(Span::styled(progress, secondary_style())),
        Line::from(Span::styled(
            text(
                app.language,
                "Fast metadata reads avoid fully loading large chat transcripts. Esc cancels.",
                "列表只读元数据，不会完整加载大会话。按 Esc 取消。",
            ),
            subtle_style(),
        )),
    ];
    let area = centered_rect_lines(58, lines.len() + 3, frame_area);
    frame.render_widget(Clear, area);
    frame.render_widget(
        content_block(text(app.language, " Loading ", " 加载中 "), lines),
        area,
    );
}

fn render_duplicate_confirm(frame: &mut Frame<'_>, app: &App, frame_area: Rect) {
    let lang = app.language;
    let options = [
        text(lang, "Import duplicate", "再导入一份"),
        text(lang, "Cancel", "取消"),
    ];
    let text_lines = vec![
        Line::from(Span::styled(
            text(lang, "Duplicate native import", "检测到重复导入"),
            accent_style(),
        )),
        Line::from(""),
        Line::from(format!("{}: {}", origin_label(app), app.import_title())),
        Line::from(app.duplicate_message.clone()),
        Line::from(""),
        Line::from(format!(
            "{}: {}",
            text(lang, "Next title", "新副本标题"),
            app.duplicate_next_title
        )),
        Line::from(""),
        button_row(&options, app.confirm_index),
        Line::from(""),
        Line::from(Span::styled(
            text(
                lang,
                "Enter confirm  Y import  N/Esc cancel",
                "Enter 确认  Y 导入  N/Esc 取消",
            ),
            subtle_style(),
        )),
    ];
    // The message wraps over a couple of lines; leave room for it.
    let area = centered_rect_lines(66, text_lines.len() + 4, frame_area);
    frame.render_widget(Clear, area);
    frame.render_widget(
        content_block(
            text(lang, " Confirm Duplicate ", " 确认重复导入 "),
            text_lines,
        ),
        area,
    );
}

fn render_filter(frame: &mut Frame<'_>, app: &App, area: Rect) {
    frame.render_widget(Clear, area);
    let lines = vec![
        Line::from(text(
            app.language,
            "Search title / project path / session id. Enter applies, Esc cancels.",
            "搜索标题 / 项目路径 / 会话 id。Enter 应用，Esc 取消。",
        )),
        Line::from(""),
        Line::from(Span::styled(
            format!(" /{} ", app.filter_input),
            selected_style(),
        )),
    ];
    frame.render_widget(
        content_block(text(app.language, " Filter ", " 筛选 "), lines),
        area,
    );
}

fn chat_bridge_logo_lines(width: u16) -> Vec<Line<'static>> {
    if width < 20 {
        return vec![Line::from(Span::styled("  CHAT BRIDGE", logo_style()))];
    }
    // The full pixel word renders 54 columns wide; keep a safety margin over
    // the bordered panel's inner width so it never wraps at the breakpoint.
    if width < 58 {
        let mut lines = pixel_logo_word("CB");
        lines.push(Line::from(Span::styled("  CHAT BRIDGE", logo_style())));
        return lines;
    }

    pixel_logo_word("CHAT BRIDGE")
}

fn pixel_logo_word(word: &str) -> Vec<Line<'static>> {
    let rows = logo_word_rows(word);
    let max_width = rows
        .iter()
        .map(|row| row.chars().count())
        .max()
        .unwrap_or(0);
    let mut lines = Vec::with_capacity(rows.len() + 1);
    for row_index in 0..=rows.len() {
        let mut spans = vec![Span::raw("  ")];
        for column in 0..=max_width {
            let filled =
                row_index < rows.len() && rows[row_index].as_bytes().get(column) == Some(&b'#');
            let shadow = row_index > 0
                && column > 0
                && rows
                    .get(row_index - 1)
                    .and_then(|row| row.as_bytes().get(column - 1))
                    == Some(&b'#');
            if filled {
                spans.push(Span::styled("█", logo_fill_style(column)));
            } else if shadow {
                spans.push(Span::styled("░", logo_shadow_style()));
            } else {
                spans.push(Span::raw(" "));
            }
        }
        lines.push(Line::from(spans));
    }
    lines
}

fn logo_word_rows(word: &str) -> [String; 5] {
    let mut rows: [String; 5] = std::array::from_fn(|_| String::new());
    for letter in word.chars() {
        let glyph = logo_glyph(letter);
        if letter == ' ' {
            for row in &mut rows {
                row.push_str("   ");
            }
            continue;
        }
        for (index, row) in rows.iter_mut().enumerate() {
            row.push_str(glyph[index]);
            row.push(' ');
        }
    }
    for row in &mut rows {
        while row.ends_with(' ') {
            row.pop();
        }
    }
    rows
}

fn logo_glyph(letter: char) -> [&'static str; 5] {
    match letter {
        'A' => [" ## ", "#  #", "####", "#  #", "#  #"],
        'B' => ["### ", "#  #", "### ", "#  #", "### "],
        'C' => [" ###", "#   ", "#   ", "#   ", " ###"],
        'D' => ["### ", "#  #", "#  #", "#  #", "### "],
        'E' => ["####", "#   ", "### ", "#   ", "####"],
        'G' => [" ###", "#   ", "# ##", "#  #", " ###"],
        'H' => ["#  #", "#  #", "####", "#  #", "#  #"],
        'I' => ["###", " # ", " # ", " # ", "###"],
        'R' => ["### ", "#  #", "### ", "# # ", "#  #"],
        'T' => ["####", " ## ", " ## ", " ## ", " ## "],
        _ => ["", "", "", "", ""],
    }
}

fn selected_style() -> Style {
    Theme::current().selected()
}

fn inactive_chip_style() -> Style {
    Theme::current().subtle().add_modifier(Modifier::BOLD)
}

fn label_style() -> Style {
    Theme::current().muted().add_modifier(Modifier::BOLD)
}

fn value_style() -> Style {
    Theme::current().secondary()
}

fn logo_style() -> Style {
    Theme::current().accent()
}

fn logo_fill_style(index: usize) -> Style {
    Theme::current().logo_fill(index)
}

fn logo_shadow_style() -> Style {
    Theme::current().logo_shadow()
}

fn text_style() -> Style {
    Theme::current().text()
}

fn muted_style() -> Style {
    Theme::current().muted()
}

fn subtle_style() -> Style {
    Theme::current().subtle()
}

fn accent_style() -> Style {
    Theme::current().accent()
}

fn secondary_style() -> Style {
    Theme::current().secondary()
}

fn success_style() -> Style {
    Theme::current().success()
}

fn danger_style() -> Style {
    Theme::current().danger()
}

fn border_style() -> Style {
    Theme::current().border()
}

fn active_border_style() -> Style {
    Theme::current().active_border()
}

fn panel(title: impl Into<String>) -> Block<'static> {
    panel_with_border(title, border_style())
}

fn active_panel(title: impl Into<String>) -> Block<'static> {
    panel_with_border(title, active_border_style())
}

fn panel_with_border(title: impl Into<String>, border: Style) -> Block<'static> {
    let title = title.into();
    let normalized = title.trim().to_ascii_lowercase();
    let title_style = if normalized.contains("error") || normalized.contains("cancelled") {
        danger_style()
    } else {
        accent_style()
    };
    Block::default()
        .title(Line::from(Span::styled(title, title_style)))
        .borders(Borders::ALL)
        .border_type(BorderType::Rounded)
        .border_style(border)
        .style(text_style())
}

fn content_block(title: impl Into<String>, lines: Vec<Line<'static>>) -> Paragraph<'static> {
    Paragraph::new(lines)
        .block(active_panel(title))
        .style(text_style())
        .wrap(Wrap { trim: false })
}

fn centered_rect(percent_x: u16, percent_y: u16, area: Rect) -> Rect {
    let vertical = Layout::default()
        .direction(Direction::Vertical)
        .constraints([
            Constraint::Percentage((100 - percent_y) / 2),
            Constraint::Percentage(percent_y),
            Constraint::Percentage((100 - percent_y) / 2),
        ])
        .split(area);
    Layout::default()
        .direction(Direction::Horizontal)
        .constraints([
            Constraint::Percentage((100 - percent_x) / 2),
            Constraint::Percentage(percent_x),
            Constraint::Percentage((100 - percent_x) / 2),
        ])
        .split(vertical[1])[1]
}

fn session_list_lines(
    session: &SessionRow,
    visible_index: usize,
    width: u16,
    language: Language,
) -> Vec<Line<'static>> {
    let project = session.project_label();
    let available = usize::from(width).saturating_sub(8).max(30);
    let project_width = available.saturating_div(3).clamp(18, 52);
    let title_width = available.saturating_sub(project_width + 4).max(18);
    // Borders + highlight symbol take 3 columns; fit line 2 into the rest.
    let inner = usize::from(width).saturating_sub(4);
    let mut details = vec![
        Span::raw("    "),
        Span::styled(
            format!("{:<7}", compact_title(&session.scope_label(), 7)),
            secondary_style(),
        ),
    ];
    let mut used = 11;
    let id_min = 6 + 12; // "  ID: " + a recognizable id prefix
    for part in [
        format!(
            "{}: {}",
            text(language, "Updated", "更新"),
            session.updated_time_label()
        ),
        format!(
            "{}: {}",
            text(language, "Started", "开始"),
            session.start_time_label()
        ),
    ] {
        let part_width = UnicodeWidthStr::width(part.as_str()) + 2;
        if used + part_width + id_min <= inner {
            details.push(Span::raw("  "));
            details.push(Span::styled(part, subtle_style()));
            used += part_width;
        }
    }
    let id_room = inner.saturating_sub(used + 6);
    if id_room >= 8 {
        details.push(Span::raw("  "));
        details.push(Span::styled(
            format!("ID: {}", compact_title(&session.session_id, id_room)),
            subtle_style(),
        ));
    }
    vec![
        Line::from(vec![
            Span::styled(format!("{:>2}. ", visible_index + 1), muted_style()),
            Span::raw(compact_title(&session.title, title_width)),
            Span::raw("  "),
            Span::styled(compact_title(&project, project_width), subtle_style()),
        ]),
        Line::from(details),
    ]
}

/// Pad to a display width (CJK characters take two columns).
fn pad_to(value: &str, width: usize) -> String {
    let current = UnicodeWidthStr::width(value);
    format!("{value}{}", " ".repeat(width.saturating_sub(current)))
}

/// Show paths under the home directory as ~/...
fn tilde(app: &App, path: &str) -> String {
    let home = app.home.to_string_lossy();
    if !home.is_empty() && path.starts_with(home.as_ref()) {
        let rest = &path[home.len()..];
        if rest.is_empty() || rest.starts_with(std::path::MAIN_SEPARATOR) {
            return format!("~{rest}");
        }
    }
    path.to_string()
}

/// A rect of `percent_x` width and just enough height for `lines` of content.
fn centered_rect_lines(percent_x: u16, lines: usize, area: Rect) -> Rect {
    let width = area.width.saturating_mul(percent_x) / 100;
    let height = (lines as u16).saturating_add(2).min(area.height);
    Rect {
        x: area.x + (area.width.saturating_sub(width)) / 2,
        y: area.y + (area.height.saturating_sub(height)) / 2,
        width,
        height,
    }
}

fn loading_progress_bar(tick: usize, width: usize) -> String {
    let width = width.max(8);
    let active = (tick % (width + 1)).max(1);
    format!("[{}{}]", "#".repeat(active), "-".repeat(width - active))
}

fn compact_title(value: &str, max_width: usize) -> String {
    if UnicodeWidthStr::width(value) <= max_width {
        return value.to_string();
    }
    if max_width <= 3 {
        // Too narrow for an ellipsis: hard-cut without ever exceeding the budget.
        let mut out = String::new();
        for ch in value.chars() {
            let ch_width = UnicodeWidthStr::width(ch.to_string().as_str());
            if UnicodeWidthStr::width(out.as_str()) + ch_width > max_width {
                break;
            }
            out.push(ch);
        }
        return out;
    }
    let mut out = String::new();
    for ch in value.chars() {
        if UnicodeWidthStr::width(out.as_str())
            + UnicodeWidthStr::width(ch.to_string().as_str())
            + 3
            > max_width
        {
            out.push_str("...");
            return out;
        }
        out.push(ch);
    }
    out
}

fn format_time_label(value: &Option<serde_json::Value>) -> String {
    value
        .as_ref()
        .and_then(parse_time_value)
        .map(|time| time.format("%Y-%m-%d %H:%M %Z").to_string())
        .unwrap_or_else(|| "-".to_string())
}

fn parse_time_value(value: &serde_json::Value) -> Option<DateTime<Local>> {
    match value {
        serde_json::Value::Number(number) => {
            if let Some(raw) = number.as_i64() {
                return local_from_unix(raw);
            }
            number.as_u64().and_then(|raw| local_from_unix(raw as i64))
        }
        serde_json::Value::String(text) => {
            if let Ok(raw) = text.parse::<i64>() {
                return local_from_unix(raw);
            }
            DateTime::parse_from_rfc3339(text)
                .ok()
                .map(|time| time.with_timezone(&Local))
        }
        _ => None,
    }
}

fn local_from_unix(raw: i64) -> Option<DateTime<Local>> {
    let utc = if raw.abs() > 10_000_000_000 {
        Utc.timestamp_millis_opt(raw).single()?
    } else {
        Utc.timestamp_opt(raw, 0).single()?
    };
    Some(utc.with_timezone(&Local))
}

fn session_detail(session: &SessionRow) -> String {
    format!(
        "Title: {}\nStarted: {}\nUpdated: {}\nID: {}\nSource: {}\nScope: {}\nProject: {}\nRaw: {}",
        session.title,
        session.start_time_label(),
        session.updated_time_label(),
        session.session_id,
        session.source_label,
        session.scope_label(),
        session.project_label(),
        session.raw_path.as_deref().unwrap_or("-")
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use ratatui::backend::TestBackend;

    fn key(code: KeyCode) -> KeyEvent {
        KeyEvent::from(code)
    }

    fn sample_session() -> SessionRow {
        SessionRow {
            source: "copilot".to_string(),
            source_label: "Copilot".to_string(),
            session_id: "very-long-session-id-that-should-not-dominate-the-session-row".to_string(),
            title: "开始全面分析方法".to_string(),
            project_path: Some("/repo/app".to_string()),
            created_at: Some(serde_json::json!("2026-06-05T04:34:00Z")),
            updated_at: Some(serde_json::json!("2026-06-05T05:45:00Z")),
            raw_path: Some("/tmp/s1.json".to_string()),
            scope: "LOCAL".to_string(),
        }
    }

    fn sample_preview() -> PreviewData {
        PreviewData {
            source_label: "Copilot".to_string(),
            session_id: "s1".to_string(),
            title: "开始全面分析方法".to_string(),
            project_path: Some("/repo/app".to_string()),
            local_project: Some("/repo/app".to_string()),
            project_exists: false,
            stats: serde_json::json!({"user": 2, "assistant": 3, "toolCalls": 3, "approxTokens": 4200, "tools": {"shell": 2, "apply_patch": 1}}),
            files_touched: vec!["/repo/app/src/auth.py".to_string()],
            first_prompt: Some("Fix login".to_string()),
            last_reply: Some("Done".to_string()),
            import_estimate: Some(ImportEstimate {
                level: "full".to_string(),
                budget: Some(120000),
                messages: 6,
                approx_tokens: 4200,
                trimmed: false,
                notes: vec![],
            }),
            ..PreviewData::default()
        }
    }

    fn sample_bundle() -> BundleRow {
        BundleRow {
            path: "/tmp/home/.chatbridge/exports/chatbridge-export-codex-t1.json".to_string(),
            name: "chatbridge-export-codex-t1.json".to_string(),
            size: 2048,
            source: Some("codex".to_string()),
            session_id: Some("t1".to_string()),
            title: Some("Debug server".to_string()),
            project_path: Some("/Users/me/app".to_string()),
            stats: Some(BundleStats {
                messages: Some(12),
                tool_calls: Some(5),
                ..BundleStats::default()
            }),
            ..BundleRow::default()
        }
    }

    fn render_buffer(app: &mut App, width: u16, height: u16) -> String {
        let backend = TestBackend::new(width, height);
        let mut terminal = Terminal::new(backend).expect("test backend");
        terminal.draw(|frame| render(frame, app)).expect("draw");
        format!("{:?}", terminal.backend().buffer())
    }

    fn detect_color_mode(vars: &[(&str, &str)]) -> ColorMode {
        ColorMode::detect_from(|key| {
            vars.iter()
                .find_map(|(name, value)| (*name == key).then(|| (*value).to_string()))
        })
    }

    #[test]
    fn color_mode_detection_respects_no_color_and_dumb_terminal() {
        assert_eq!(detect_color_mode(&[("NO_COLOR", "1")]), ColorMode::Never);
        assert_eq!(detect_color_mode(&[("TERM", "dumb")]), ColorMode::Never);
        // Per no-color.org an EMPTY NO_COLOR must be ignored.
        assert_eq!(detect_color_mode(&[("NO_COLOR", "")]), ColorMode::Ansi16);
        // An explicit app-specific opt-in beats the generic NO_COLOR.
        assert_eq!(
            detect_color_mode(&[("NO_COLOR", "1"), ("CHATBRIDGE_COLOR", "truecolor")]),
            ColorMode::Truecolor
        );
        assert_eq!(
            detect_color_mode(&[("NO_COLOR", "1"), ("CHATBRIDGE_COLOR", "auto")]),
            ColorMode::Never
        );
    }

    #[test]
    fn color_mode_detection_picks_terminal_capabilities_and_overrides() {
        assert_eq!(
            detect_color_mode(&[("TERM", "xterm-256color")]),
            ColorMode::Ansi256
        );
        assert_eq!(
            detect_color_mode(&[("TERM_PROGRAM", "Apple_Terminal")]),
            ColorMode::Ansi256
        );
        assert_eq!(
            detect_color_mode(&[("COLORTERM", "truecolor")]),
            ColorMode::Truecolor
        );
        assert_eq!(
            detect_color_mode(&[("CHATBRIDGE_COLOR", "ansi16"), ("COLORTERM", "truecolor")]),
            ColorMode::Ansi16
        );
        assert_eq!(
            detect_color_mode(&[("CHATBRIDGE_COLOR", "never"), ("TERM", "xterm-256color")]),
            ColorMode::Never
        );
    }

    #[test]
    fn selected_style_uses_reverse_without_fixed_background() {
        let style = selected_style();

        assert_eq!(style.bg, None);
        assert!(style.add_modifier.contains(Modifier::REVERSED));
        assert!(style.add_modifier.contains(Modifier::BOLD));
    }

    #[test]
    fn muted_and_border_styles_avoid_dim_for_light_terminals() {
        assert!(!muted_style().add_modifier.contains(Modifier::DIM));
        assert!(!border_style().add_modifier.contains(Modifier::DIM));
        assert_eq!(border_style().bg, None);
    }

    #[test]
    fn logo_uses_pixel_blocks_without_background() {
        let lines = chat_bridge_logo_lines(80);
        let text = lines
            .iter()
            .map(|line| {
                line.spans
                    .iter()
                    .map(|span| span.content.as_ref())
                    .collect::<String>()
            })
            .collect::<Vec<_>>()
            .join("\n");

        assert!(text.contains("█"));
        assert!(text.contains("░"));
        for line in lines {
            for span in line.spans {
                assert_eq!(span.style.bg, None);
            }
        }
    }

    #[test]
    fn home_render_does_not_force_terminal_background() {
        let mut app = App::new(PathBuf::from("/tmp/home"));

        let buffer = render_buffer(&mut app, 110, 30);

        assert!(!buffer.contains("bg: Some"));
        assert!(buffer.contains("ChatBridge"));
    }

    #[test]
    fn tab_switches_source_and_left_right_do_not() {
        let mut app = App::new(PathBuf::from("/tmp/home"));

        let _ = handle_key(&mut app, key(KeyCode::Tab));
        assert_eq!(app.source(), Source::Codex);

        let _ = handle_key(&mut app, key(KeyCode::Left));
        assert_eq!(app.source(), Source::Codex);

        let _ = handle_key(&mut app, key(KeyCode::Right));
        assert_eq!(app.source(), Source::Codex);
    }

    #[test]
    fn action_label_is_view_recent_sessions() {
        assert_eq!(
            ChatAction::Recent.label_for(Language::English),
            "View Recent Sessions"
        );
    }

    #[test]
    fn native_import_session_enter_opens_preview() {
        let mut app = App::new(PathBuf::from("/tmp/home"));
        app.view = View::Sessions;
        app.pending_action = ChatAction::Native;
        app.sessions = vec![sample_session()];

        let command = handle_key(&mut app, key(KeyCode::Enter));

        match command {
            UiCommand::LoadPreview { request, purpose } => {
                assert_eq!(purpose, PreviewPurpose::Import);
                assert!(matches!(
                    request,
                    ImportRequest::Session {
                        source: Source::Copilot,
                        ..
                    }
                ));
            }
            other => panic!("unexpected command: {other:?}"),
        }
        apply_worker_result(
            &mut app,
            WorkerResult::Preview(PreviewPurpose::Import, Ok(Box::new(sample_preview()))),
        );
        assert_eq!(app.view(), &View::SessionPreview);
    }

    #[test]
    fn session_preview_continue_goes_target_cancel_returns_sessions() {
        let mut app = App::new(PathBuf::from("/tmp/home"));
        app.view = View::SessionPreview;
        app.pending_action = ChatAction::Native;
        app.sessions = vec![sample_session()];

        let _ = handle_key(&mut app, key(KeyCode::Enter));
        assert_eq!(app.view(), &View::Target);

        app.view = View::SessionPreview;
        app.preview_confirm_index = 1;
        let _ = handle_key(&mut app, key(KeyCode::Enter));
        assert_eq!(app.view(), &View::Sessions);
    }

    #[test]
    fn load_more_row_requests_larger_session_limit() {
        let mut app = App::new(PathBuf::from("/tmp/home"));
        app.view = View::Sessions;
        app.pending_action = ChatAction::Recent;
        app.sessions = vec![sample_session()];
        app.session_limit = 50;
        app.session_total = 75;
        app.session_has_more = true;
        app.selected_index = 1;

        let command = handle_key(&mut app, key(KeyCode::Enter));

        match command {
            UiCommand::LoadSessions { limit, .. } => assert_eq!(limit, 100),
            other => panic!("unexpected command: {other:?}"),
        }
    }

    #[test]
    fn filter_matches_project_path() {
        let session = sample_session();

        assert!(session.matches_filter("/repo/app"));
        assert!(session.matches_filter("session-row"));
        assert!(!session.matches_filter("/missing/path"));
    }

    #[test]
    fn home_render_contains_oh_my_logo_brand() {
        let mut app = App::new(PathBuf::from("/tmp/home"));

        let buffer = render_buffer(&mut app, 110, 30);

        assert!(buffer.contains("ChatBridge"));
        assert!(buffer.contains("████"));
        assert!(buffer.contains("░"));
        assert!(buffer.contains("ready"));
        assert!(buffer.contains("AI history bridge"));
        assert!(!buffer.contains("█▀▀"));
        assert!(!buffer.contains("local logs"));
        assert!(!buffer.contains("/ ____"));
        assert!(!buffer.contains("[ C ]==[ B ]"));
    }

    #[test]
    fn compact_home_render_uses_mini_logo() {
        let mut app = App::new(PathBuf::from("/tmp/home"));

        let buffer = render_buffer(&mut app, 80, 24);

        assert!(buffer.contains("ChatBridge"));
        assert!(buffer.contains("██"));
        assert!(buffer.contains("░"));
        assert!(buffer.contains("CHAT BRIDGE"));
        assert!(buffer.contains("AI history bridge"));
        assert!(!buffer.contains("Connection Details"));
    }

    #[test]
    fn footer_shows_version_and_language_toggle_switches_panel_copy() {
        let mut app = App::new(PathBuf::from("/tmp/home"));

        let buffer = render_buffer(&mut app, 110, 30);
        assert!(buffer.contains(&format!("v{VERSION}")));

        let _ = handle_key(&mut app, key(KeyCode::Char('t')));
        let buffer = render_buffer(&mut app, 110, 30);

        assert!(buffer.contains("查看最近会话"));
        assert!(buffer.contains("语言"));
    }

    #[test]
    fn target_selection_excludes_same_tool_as_source() {
        let mut app = App::new(PathBuf::from("/tmp/home"));
        app.view = View::Target;
        app.pending_action = ChatAction::Native;
        app.source_index = 0;
        app.sessions = vec![sample_session()];

        assert_eq!(app.target(), Target::Codex);
        let _ = handle_key(&mut app, key(KeyCode::Right));
        assert_eq!(app.target(), Target::Claude);
        let _ = handle_key(&mut app, key(KeyCode::Right));
        assert_eq!(app.target(), Target::Export);
        let _ = handle_key(&mut app, key(KeyCode::Right));
        assert_eq!(app.target(), Target::Codex);

        let command = handle_key(&mut app, key(KeyCode::Enter));
        assert!(matches!(command, UiCommand::None));
        app.confirm_index = 1;
        let command = handle_key(&mut app, key(KeyCode::Enter));
        match command {
            UiCommand::NativeImport { target, .. } => assert_ne!(target, Target::Copilot),
            other => panic!("unexpected command: {other:?}"),
        }
    }

    #[test]
    fn native_targets_cover_all_cross_tool_imports() {
        assert_eq!(
            Target::available_for(Source::Copilot),
            [Target::Codex, Target::Claude, Target::Export]
        );
        assert_eq!(
            Target::available_for(Source::Codex),
            [Target::Claude, Target::Copilot, Target::Export]
        );
        assert_eq!(
            Target::available_for(Source::Claude),
            [Target::Codex, Target::Copilot, Target::Export]
        );
    }

    #[test]
    fn export_target_enter_emits_export_command() {
        let mut app = App::new(PathBuf::from("/tmp/home"));
        app.view = View::Target;
        app.pending_action = ChatAction::Native;
        app.sessions = vec![sample_session()];
        app.target_index = 2;

        assert_eq!(app.target(), Target::Export);
        let command = handle_key(&mut app, key(KeyCode::Enter));

        match command {
            UiCommand::ExportSession { source, session_id } => {
                assert_eq!(source, Source::Copilot);
                assert!(session_id.contains("very-long-session-id"));
            }
            other => panic!("unexpected command: {other:?}"),
        }
    }

    #[test]
    fn export_result_returns_to_sessions() {
        let mut app = App::new(PathBuf::from("/tmp/home"));
        app.sessions = vec![sample_session()];

        apply_worker_result(
            &mut app,
            WorkerResult::Export(Ok(
                "Exported Copilot session s1 to /tmp/bundle.json".to_string()
            )),
        );

        assert_eq!(app.view(), &View::Result);
        assert!(app.result_text.contains("/tmp/bundle.json"));
        let _ = handle_key(&mut app, key(KeyCode::Esc));
        assert_eq!(app.view(), &View::Sessions);
    }

    #[test]
    fn session_detail_escape_returns_to_session_list() {
        let mut app = App::new(PathBuf::from("/tmp/home"));
        app.view = View::Sessions;
        app.pending_action = ChatAction::Recent;
        app.sessions = vec![sample_session()];

        let command = handle_key(&mut app, key(KeyCode::Enter));
        assert!(matches!(
            command,
            UiCommand::LoadPreview {
                purpose: PreviewPurpose::Detail,
                ..
            }
        ));
        apply_worker_result(
            &mut app,
            WorkerResult::Preview(PreviewPurpose::Detail, Ok(Box::new(sample_preview()))),
        );
        assert_eq!(app.view(), &View::Result);
        assert!(app.result_text.contains("3 tool calls"));
        assert!(app.result_text.contains("/repo/app/src/auth.py"));

        let _ = handle_key(&mut app, key(KeyCode::Esc));
        assert_eq!(app.view(), &View::Sessions);
    }

    #[test]
    fn session_scoped_results_escape_returns_to_session_list() {
        let mut app = App::new(PathBuf::from("/tmp/home"));
        app.view = View::Loading;
        app.pending_action = ChatAction::Handoff;
        app.sessions = vec![sample_session()];

        apply_worker_result(&mut app, WorkerResult::Handoff(Ok("handoff".to_string())));
        assert_eq!(app.view(), &View::Result);
        let _ = handle_key(&mut app, key(KeyCode::Esc));
        assert_eq!(app.view(), &View::Sessions);

        apply_worker_result(&mut app, WorkerResult::Native(Ok("native".to_string())));
        assert_eq!(app.view(), &View::Result);
        let _ = handle_key(&mut app, key(KeyCode::Esc));
        assert_eq!(app.view(), &View::Sessions);
    }

    #[test]
    fn path_doctor_result_escape_returns_home() {
        let mut app = App::new(PathBuf::from("/tmp/home"));

        apply_worker_result(&mut app, WorkerResult::Paths(Ok("paths".to_string())));
        assert_eq!(app.view(), &View::Result);
        let _ = handle_key(&mut app, key(KeyCode::Esc));
        assert_eq!(app.view(), &View::Home);
    }

    #[test]
    fn path_action_opens_direct_path_setup_form() {
        let mut app = App::new(PathBuf::from("/tmp/home"));
        app.action_index = ChatAction::Paths.index();

        let command = handle_key(&mut app, key(KeyCode::Enter));

        assert!(matches!(command, UiCommand::None));
        assert_eq!(app.view(), &View::PathSetup);
        let buffer = render_buffer(&mut app, 110, 30);
        assert!(buffer.contains("Copilot workspaceStorage"));
        assert!(buffer.contains("Codex home"));
        assert!(buffer.contains("Claude home"));
    }

    #[test]
    fn path_setup_accepts_input_and_saves_selected_target() {
        let mut app = App::new(PathBuf::from("/tmp/home"));
        open_path_setup(&mut app);
        let _ = handle_key(&mut app, key(KeyCode::Down));
        for ch in "/tmp/custom-codex".chars() {
            let _ = handle_key(&mut app, key(KeyCode::Char(ch)));
        }

        let command = handle_key(&mut app, key(KeyCode::Enter));

        match command {
            UiCommand::SetPath { target, value } => {
                assert_eq!(target, PathTarget::CodexHome);
                assert_eq!(value, "/tmp/custom-codex");
            }
            other => panic!("unexpected command: {other:?}"),
        }
    }

    #[test]
    fn path_setup_question_mark_types_into_input_and_ctrl_d_runs_doctor() {
        let mut app = App::new(PathBuf::from("/tmp/home"));
        open_path_setup(&mut app);

        let command = handle_key(&mut app, key(KeyCode::Char('?')));
        assert!(matches!(command, UiCommand::None));
        assert_eq!(app.path_input, "?");

        let command = handle_key(
            &mut app,
            KeyEvent::new(KeyCode::Char('d'), KeyModifiers::CONTROL),
        );
        assert!(matches!(command, UiCommand::PathsDoctor));
    }

    #[test]
    fn path_setup_t_is_typeable_not_language_toggle() {
        let mut app = App::new(PathBuf::from("/tmp/home"));
        open_path_setup(&mut app);

        let _ = handle_key(&mut app, key(KeyCode::Char('t')));

        assert_eq!(app.path_input, "t");
        assert_eq!(app.language, Language::English);
    }

    #[test]
    fn ctrl_c_quits_from_any_view() {
        let mut app = App::new(PathBuf::from("/tmp/home"));
        for view in [View::Home, View::Sessions, View::Loading, View::PathSetup] {
            app.view = view;
            let command = handle_key(
                &mut app,
                KeyEvent::new(KeyCode::Char('c'), KeyModifiers::CONTROL),
            );
            assert!(matches!(command, UiCommand::Quit), "view {view:?}");
        }
    }

    #[test]
    fn loading_escape_cancels_and_restores_back_view() {
        let mut app = App::new(PathBuf::from("/tmp/home"));
        app.view = View::Sessions;
        app.sessions = vec![sample_session()];
        let command = UiCommand::BuildHandoff {
            source: Source::Copilot,
            target: Target::Codex,
            session_id: "s1".to_string(),
        };
        prepare_loading(&mut app, &command);
        assert_eq!(app.view(), &View::Loading);
        let (tx, rx) = mpsc::channel();
        app.worker_rx = Some(rx);

        let _ = handle_key(&mut app, key(KeyCode::Esc));

        assert_eq!(app.view(), &View::Result);
        assert!(app.result_title.contains("Cancelled"));
        assert!(app.worker_rx.is_none());
        // A late worker result must be dropped, not applied.
        let _ = tx.send(WorkerResult::Handoff(Ok("late".to_string())));
        drain_worker(&mut app);
        assert_eq!(app.view(), &View::Result);
        assert!(!app.result_text.contains("late"));
        let _ = handle_key(&mut app, key(KeyCode::Esc));
        assert_eq!(app.view(), &View::Sessions);
    }

    #[test]
    fn uppercase_home_hotkeys_match_advertised_chips() {
        let mut app = App::new(PathBuf::from("/tmp/home"));

        let command = handle_key(&mut app, key(KeyCode::Char('L')));

        match command {
            UiCommand::LoadSessions { action, .. } => assert_eq!(action, ChatAction::Recent),
            other => panic!("unexpected command: {other:?}"),
        }
    }

    #[test]
    fn confirm_native_y_applies_and_n_cancels() {
        let mut app = App::new(PathBuf::from("/tmp/home"));
        app.view = View::ConfirmNative;
        app.pending_action = ChatAction::Native;
        app.sessions = vec![sample_session()];

        let command = handle_key(&mut app, key(KeyCode::Char('y')));
        match command {
            UiCommand::NativeImport { apply, .. } => assert!(apply),
            other => panic!("unexpected command: {other:?}"),
        }

        app.view = View::ConfirmNative;
        let command = handle_key(&mut app, key(KeyCode::Char('n')));
        assert!(matches!(command, UiCommand::None));
        assert_eq!(app.view(), &View::Target);
    }

    #[test]
    fn confirm_dialogs_render_button_rows_with_confirm_keys() {
        let mut app = App::new(PathBuf::from("/tmp/home"));
        app.view = View::ConfirmNative;
        app.pending_action = ChatAction::Native;
        app.sessions = vec![sample_session()];

        let buffer = render_buffer(&mut app, 120, 32);
        assert!(buffer.contains("[ Dry run ]"));
        assert!(buffer.contains("[ Apply ]"));
        assert!(buffer.contains("[ Cancel ]"));
        assert!(buffer.contains("Import into project"));

        app.view = View::SessionPreview;
        let buffer = render_buffer(&mut app, 120, 32);
        assert!(buffer.contains("[ Continue ]"));
        assert!(buffer.contains("[ Cancel ]"));

        app.view = View::ConfirmDuplicate;
        app.duplicate_message = "Duplicate native import detected".to_string();
        app.duplicate_next_title = "Copy (1)".to_string();
        let buffer = render_buffer(&mut app, 120, 32);
        assert!(buffer.contains("[ Import duplicate ]"));
        assert!(buffer.contains("[ Cancel ]"));
    }

    #[test]
    fn compact_title_never_exceeds_tiny_budget() {
        assert!(UnicodeWidthStr::width(compact_title("全面分析", 2).as_str()) <= 2);
        assert!(UnicodeWidthStr::width(compact_title("abcdef", 3).as_str()) <= 3);
        assert!(UnicodeWidthStr::width(compact_title("全面分析方法", 5).as_str()) <= 5);
    }

    #[test]
    fn result_scroll_is_clamped_to_content() {
        let mut app = App::new(PathBuf::from("/tmp/home"));
        app.view = View::Result;
        app.result_text = "line1\nline2\nline3".to_string();

        for _ in 0..10 {
            let _ = handle_key(&mut app, key(KeyCode::Down));
        }

        assert_eq!(app.result_scroll, 2);
    }

    #[test]
    fn session_preview_modal_does_not_repeat_title_inside_body() {
        let mut app = App::new(PathBuf::from("/tmp/home"));
        app.view = View::SessionPreview;
        app.pending_action = ChatAction::Native;
        app.sessions = vec![sample_session()];

        let buffer = render_buffer(&mut app, 110, 30);

        assert_eq!(buffer.matches("Chat Preview").count(), 1);
    }

    #[test]
    fn session_list_renders_two_line_record_with_title_path_time_and_id() {
        let mut app = App::new(PathBuf::from("/tmp/home"));
        app.view = View::Sessions;
        app.pending_action = ChatAction::Recent;
        app.sessions = vec![sample_session()];
        app.session_total = 1;
        app.session_limit = 50;

        let buffer = render_buffer(&mut app, 130, 28);

        assert!(buffer.contains("LOCAL"));
        assert!(buffer.contains("2026"));
        assert!(buffer.contains("开始全面分析方法"));
        assert!(buffer.contains("/repo/app"));
        assert!(buffer.contains("ID: very-long-session-id"));
    }

    #[test]
    fn loading_render_shows_progress_bar_and_fast_path_hint() {
        let mut app = App::new(PathBuf::from("/tmp/home"));
        app.view = View::Loading;
        app.session_limit = 50;
        app.tick = 6;
        app.loading_message =
            "Scanning GitHub Copilot index with fast metadata path...".to_string();

        let buffer = render_buffer(&mut app, 100, 26);

        assert!(buffer.contains("Scanning GitHub Copilot index"));
        assert!(buffer.contains("["));
        assert!(buffer.contains("]"));
        assert!(buffer.contains("fast metadata"));
    }

    #[test]
    fn native_import_enter_returns_load_command_then_loading_state() {
        let mut app = App::new(PathBuf::from("/tmp/home"));
        app.action_index = 2;

        let command = handle_key(&mut app, key(KeyCode::Enter));

        match command {
            UiCommand::LoadSessions {
                action,
                source,
                limit,
                ..
            } => {
                assert_eq!(action, ChatAction::Native);
                assert_eq!(source, Source::Copilot);
                assert_eq!(limit, DEFAULT_SESSION_LIMIT);
            }
            other => panic!("unexpected command: {other:?}"),
        }
        prepare_loading(&mut app, &command);
        assert_eq!(app.view(), &View::Loading);
        assert!(app
            .loading_message()
            .contains("Loading GitHub Copilot sessions"));
    }

    #[test]
    fn duplicate_native_import_opens_confirm_modal() {
        let mut app = App::new(PathBuf::from("/tmp/home"));
        app.sessions = vec![sample_session()];
        app.pending_action = ChatAction::Native;
        apply_worker_result(
            &mut app,
            WorkerResult::Native(Err(ApiError {
                kind: "duplicate".to_string(),
                message:
                    "Duplicate native import detected for [Imported from Copilot] 开始全面分析方法."
                        .to_string(),
                next_title: Some("[Imported from Copilot] 开始全面分析方法 (1)".to_string()),
            })),
        );

        assert_eq!(app.view(), &View::ConfirmDuplicate);
        assert!(app.duplicate_next_title.contains("(1)"));
    }

    #[test]
    fn unicode_result_renders_with_test_backend() {
        let backend = TestBackend::new(100, 28);
        let mut terminal = Terminal::new(backend).expect("test backend");
        let mut app = App::new(PathBuf::from("/tmp/home"));
        app.view = View::Result;
        app.result_title = "Result".to_string();
        app.result_text = "Duplicate native import detected for [Imported from Copilot] 开始全面分析方法 into codex project /repo/app.".to_string();

        terminal
            .draw(|frame| render(frame, &mut app))
            .expect("draw");

        let buffer = format!("{:?}", terminal.backend().buffer());
        assert!(buffer.contains("Result"));
        assert!(buffer.contains("Duplicate"));
    }

    #[test]
    fn bundle_flow_imports_into_any_target_with_edited_project() {
        let mut app = App::new(PathBuf::from("/tmp/home"));

        let command = handle_key(&mut app, key(KeyCode::Char('i')));
        assert!(matches!(command, UiCommand::LoadBundles));
        apply_worker_result(
            &mut app,
            WorkerResult::Bundles(Ok(BundlesData {
                bundles: vec![sample_bundle()],
                dirs: vec!["/tmp/home/.chatbridge/exports".to_string()],
                export_dir: "/tmp/home/.chatbridge/exports".to_string(),
            })),
        );
        assert_eq!(app.view(), &View::Bundles);
        let buffer = render_buffer(&mut app, 120, 30);
        assert!(buffer.contains("Debug server"));
        assert!(buffer.contains("12 msgs"));
        assert!(buffer.contains("Type a bundle path"));

        let command = handle_key(&mut app, key(KeyCode::Enter));
        match command {
            UiCommand::LoadPreview { request, purpose } => {
                assert_eq!(purpose, PreviewPurpose::Import);
                assert_eq!(
                    request,
                    ImportRequest::Bundle {
                        path: sample_bundle().path
                    }
                );
            }
            other => panic!("unexpected command: {other:?}"),
        }
        apply_worker_result(
            &mut app,
            WorkerResult::Preview(PreviewPurpose::Import, Ok(Box::new(sample_preview()))),
        );
        assert_eq!(app.view(), &View::SessionPreview);
        let buffer = render_buffer(&mut app, 120, 34);
        assert!(buffer.contains("not on this machine"));

        let _ = handle_key(&mut app, key(KeyCode::Enter));
        assert_eq!(app.view(), &View::Target);
        assert_eq!(
            app.targets(),
            [Target::Codex, Target::Claude, Target::Copilot]
        );
        let _ = handle_key(&mut app, key(KeyCode::Right));
        assert_eq!(app.target(), Target::Claude);
        let _ = handle_key(&mut app, key(KeyCode::Enter));
        assert_eq!(app.view(), &View::ConfirmNative);
        let buffer = render_buffer(&mut app, 120, 34);
        assert!(buffer.contains("does not exist on this machine"));

        let _ = handle_key(&mut app, key(KeyCode::Char('e')));
        assert!(app.editing_project);
        assert_eq!(app.project_input, "/repo/app");
        for _ in 0.."/repo/app".len() {
            let _ = handle_key(&mut app, key(KeyCode::Backspace));
        }
        for ch in "~/work/app".chars() {
            let _ = handle_key(&mut app, key(KeyCode::Char(ch)));
        }
        let _ = handle_key(&mut app, key(KeyCode::Enter));
        assert!(!app.editing_project);
        assert_eq!(app.project_override.as_deref(), Some("/tmp/home/work/app"));

        let command = handle_key(&mut app, key(KeyCode::Enter));
        match command {
            UiCommand::NativeImport {
                request,
                target,
                apply,
                allow_duplicate,
                project,
            } => {
                assert_eq!(
                    request,
                    ImportRequest::Bundle {
                        path: sample_bundle().path
                    }
                );
                assert_eq!(target, Target::Claude);
                assert!(!apply);
                assert!(!allow_duplicate);
                assert_eq!(project.as_deref(), Some("/tmp/home/work/app"));
            }
            other => panic!("unexpected command: {other:?}"),
        }
        apply_worker_result(&mut app, WorkerResult::Native(Ok("DRY RUN".to_string())));
        let _ = handle_key(&mut app, key(KeyCode::Esc));
        assert_eq!(app.view(), &View::Bundles);
    }

    #[test]
    fn typed_bundle_path_expands_home_and_bad_bundle_returns_to_list() {
        let mut app = App::new(PathBuf::from("/tmp/home"));
        app.view = View::Bundles;

        let _ = handle_key(&mut app, key(KeyCode::Enter));
        assert_eq!(app.view(), &View::BundlePath);
        let _ = handle_key(&mut app, key(KeyCode::Char('t')));
        assert_eq!(app.bundle_input, "t");
        app.bundle_input = "~/Downloads/b.json".to_string();
        let command = handle_key(&mut app, key(KeyCode::Enter));
        match command {
            UiCommand::LoadPreview { request, .. } => {
                assert_eq!(
                    request,
                    ImportRequest::Bundle {
                        path: "/tmp/home/Downloads/b.json".to_string()
                    }
                );
            }
            other => panic!("unexpected command: {other:?}"),
        }
        apply_worker_result(
            &mut app,
            WorkerResult::Preview(
                PreviewPurpose::Import,
                Err(ApiError {
                    kind: "error".to_string(),
                    message: "Not a ChatBridge bundle".to_string(),
                    next_title: None,
                }),
            ),
        );
        assert_eq!(app.view(), &View::Result);
        assert!(app.result_text.contains("Not a ChatBridge bundle"));
        let _ = handle_key(&mut app, key(KeyCode::Esc));
        assert_eq!(app.view(), &View::Bundles);
    }

    #[test]
    fn duplicate_bundle_import_keeps_bundle_request() {
        let mut app = App::new(PathBuf::from("/tmp/home"));
        app.bundle_origin = Some(sample_bundle());
        app.view = View::ConfirmDuplicate;

        let command = handle_key(&mut app, key(KeyCode::Char('y')));
        match command {
            UiCommand::NativeImport {
                request,
                allow_duplicate,
                apply,
                ..
            } => {
                assert!(matches!(request, ImportRequest::Bundle { .. }));
                assert!(allow_duplicate && apply);
            }
            other => panic!("unexpected command: {other:?}"),
        }
    }

    #[test]
    fn session_import_without_preview_still_continues() {
        let mut app = App::new(PathBuf::from("/tmp/home"));
        app.view = View::Sessions;
        app.pending_action = ChatAction::Native;
        app.sessions = vec![sample_session()];
        let _ = handle_key(&mut app, key(KeyCode::Enter));
        apply_worker_result(
            &mut app,
            WorkerResult::Preview(
                PreviewPurpose::Import,
                Err(ApiError {
                    kind: "timeout".to_string(),
                    message: "timed out".to_string(),
                    next_title: None,
                }),
            ),
        );
        assert_eq!(app.view(), &View::SessionPreview);
        let buffer = render_buffer(&mut app, 120, 34);
        assert!(buffer.contains("Preview unavailable"));
        let _ = handle_key(&mut app, key(KeyCode::Char('y')));
        assert_eq!(app.view(), &View::Target);
        assert_eq!(app.target(), Target::Codex);
    }

    #[test]
    fn export_goes_to_chatbridge_exports_directory() {
        let arg = export_dir_arg(std::path::Path::new("/tmp/home"));
        assert!(arg.ends_with(std::path::MAIN_SEPARATOR));
        assert!(arg.contains(".chatbridge"));
        assert!(arg.contains("exports"));
    }

    #[test]
    fn language_detection_prefers_override_then_locale() {
        let detect = |vars: &[(&str, &str)]| {
            Language::detect_from(|key| {
                vars.iter()
                    .find_map(|(name, value)| (*name == key).then(|| (*value).to_string()))
            })
        };
        assert_eq!(detect(&[("LANG", "zh_CN.UTF-8")]), Language::Chinese);
        assert_eq!(detect(&[("LANG", "en_US.UTF-8")]), Language::English);
        assert_eq!(
            detect(&[("LC_ALL", "C"), ("LANG", "zh_TW.UTF-8")]),
            Language::Chinese
        );
        assert_eq!(
            detect(&[("CHATBRIDGE_LANG", "en"), ("LANG", "zh_CN.UTF-8")]),
            Language::English
        );
        assert_eq!(detect(&[("CHATBRIDGE_LANG", "zh")]), Language::Chinese);
        assert_eq!(detect(&[]), Language::English);
    }

    #[test]
    fn base64_and_osc52_payload() {
        assert_eq!(base64_encode(b""), "");
        assert_eq!(base64_encode(b"f"), "Zg==");
        assert_eq!(base64_encode(b"fo"), "Zm8=");
        assert_eq!(base64_encode(b"foo"), "Zm9v");
        assert_eq!(base64_encode(b"foobar"), "Zm9vYmFy");
        let (payload, truncated) = osc52_payload("hi");
        assert_eq!(payload, "\x1b]52;c;aGk=\x07");
        assert!(!truncated);
        let long = "界".repeat(OSC52_MAX_BYTES); // 3 bytes per char, must cut on a char boundary
        let (payload, truncated) = osc52_payload(&long);
        assert!(truncated);
        assert!(payload.len() < OSC52_MAX_BYTES * 2);
    }

    #[test]
    fn result_view_pages_and_copies() {
        let mut app = App::new(PathBuf::from("/tmp/home"));
        app.view = View::Result;
        app.result_text = (0..40)
            .map(|i| format!("line{i}"))
            .collect::<Vec<_>>()
            .join("\n");

        let _ = handle_key(&mut app, key(KeyCode::PageDown));
        assert_eq!(app.result_scroll, 10);
        let _ = handle_key(&mut app, key(KeyCode::End));
        assert_eq!(app.result_scroll, 39);
        let _ = handle_key(&mut app, key(KeyCode::Home));
        assert_eq!(app.result_scroll, 0);
        match handle_key(&mut app, key(KeyCode::Char('c'))) {
            UiCommand::CopyText(text) => assert!(text.starts_with("line0")),
            other => panic!("unexpected command: {other:?}"),
        }
    }

    #[test]
    fn home_shows_detected_history_folders_and_bundle_action() {
        let mut app = App::new(PathBuf::from("/tmp/home"));
        app.source_status = vec![
            SourceStatus {
                source: "copilot".to_string(),
                path: "/x".to_string(),
                exists: false,
            },
            SourceStatus {
                source: "codex".to_string(),
                path: "/y".to_string(),
                exists: true,
            },
            SourceStatus {
                source: "claude".to_string(),
                path: "/z".to_string(),
                exists: true,
            },
        ];

        let buffer = render_buffer(&mut app, 120, 32);

        assert!(buffer.contains("Codex ✓"));
        assert!(buffer.contains("Copilot ✗"));
        assert!(buffer.contains("Import JSON Bundle"));
    }

    #[test]
    fn chinese_mode_translates_confirm_and_target_views() {
        let mut app = App::new(PathBuf::from("/tmp/home"));
        app.language = Language::Chinese;
        app.sessions = vec![sample_session()];
        app.pending_action = ChatAction::Native;
        app.view = View::Target;
        let buffer = render_buffer(&mut app, 120, 30);
        assert!(buffer.contains("选择目标"));
        app.view = View::ConfirmNative;
        let buffer = render_buffer(&mut app, 120, 30);
        assert!(buffer.contains("预演"));
        assert!(buffer.contains("导入到项目"));
    }

    #[test]
    fn escape_on_dashboard_does_not_quit_but_q_does() {
        let mut app = App::new(PathBuf::from("/tmp/home"));
        assert!(matches!(
            handle_key(&mut app, key(KeyCode::Esc)),
            UiCommand::None
        ));
        assert!(matches!(
            handle_key(&mut app, key(KeyCode::Char('q'))),
            UiCommand::Quit
        ));
    }

    #[test]
    fn narrow_terminal_gives_lists_the_full_width() {
        let mut app = App::new(PathBuf::from("/tmp/home"));
        app.view = View::Sessions;
        app.pending_action = ChatAction::Recent;
        app.sessions = vec![sample_session()];

        let narrow = render_buffer(&mut app, 80, 24);
        assert!(!narrow.contains("Actions"));
        assert!(narrow.contains("开始全面分析方法"));
        app.view = View::Home;
        let home = render_buffer(&mut app, 80, 24);
        assert!(home.contains("Actions"));
    }

    #[test]
    fn filter_mode_updates_session_filter() {
        let mut app = App::new(PathBuf::from("/tmp/home"));
        app.view = View::Sessions;
        app.sessions = vec![sample_session()];

        assert!(matches!(
            handle_key(&mut app, key(KeyCode::Char('/'))),
            UiCommand::None
        ));
        assert!(app.filtering);
        let _ = handle_key(&mut app, key(KeyCode::Char('a')));
        let _ = handle_key(&mut app, key(KeyCode::Enter));

        assert_eq!(app.filter, "a");
        assert!(!app.filtering);
    }
}
