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
    Paths,
}

impl ChatAction {
    fn all() -> [ChatAction; 4] {
        [
            ChatAction::Recent,
            ChatAction::Handoff,
            ChatAction::Native,
            ChatAction::Paths,
        ]
    }

    fn label_for(self, language: Language) -> &'static str {
        if language == Language::Chinese {
            return match self {
                ChatAction::Recent => "查看最近会话",
                ChatAction::Handoff => "生成接续提示",
                ChatAction::Native => "原生导入",
                ChatAction::Paths => "路径诊断与设置",
            };
        }
        match self {
            ChatAction::Recent => "View Recent Sessions",
            ChatAction::Handoff => "Prompt Handoff",
            ChatAction::Native => "Native Import",
            ChatAction::Paths => "Paths & Setup",
        }
    }

    fn description_for(self, language: Language) -> &'static str {
        if language == Language::Chinese {
            return match self {
                ChatAction::Recent => "浏览已恢复的会话，不读取完整正文。",
                ChatAction::Handoff => "为另一个 Agent 生成干净的接续提示。",
                ChatAction::Native => "写入带备份保护的原生历史会话。",
                ChatAction::Paths => "检查历史路径，并显示手动设置命令。",
            };
        }
        match self {
            ChatAction::Recent => "Browse recovered sessions without loading full bodies.",
            ChatAction::Handoff => "Create a clean continuation prompt for another agent.",
            ChatAction::Native => "Write a backup-protected synthetic native session.",
            ChatAction::Paths => "Inspect history paths and show manual setup commands.",
        }
    }

    fn hotkey(self) -> &'static str {
        match self {
            ChatAction::Recent => "L",
            ChatAction::Handoff => "H",
            ChatAction::Native => "N",
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
        source: Source,
        target: Target,
        session_id: String,
        apply: bool,
        allow_duplicate: bool,
    },
    ExportSession {
        source: Source,
        session_id: String,
    },
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
    tick: usize,
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
            tick: 0,
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

    fn target(&self) -> Target {
        let targets = Target::available_for(self.source());
        targets[self.target_index % targets.len()]
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
    let mut terminal = setup_terminal()?;
    let result = run_loop(&mut terminal, App::new(home));
    restore_terminal(&mut terminal)?;
    result
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
                    other => begin_command(&mut app, other),
                }
            }
        }
        app.tick = app.tick.wrapping_add(1);
    }
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
    // The language toggle stays out of text-input views so 't' remains typeable.
    if app.view != View::PathSetup && matches!(key.code, KeyCode::Char('t') | KeyCode::Char('T')) {
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
        KeyCode::Char('q') | KeyCode::Esc => UiCommand::Quit,
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
        KeyCode::Char('l') | KeyCode::Char('L') => {
            app.action_index = 0;
            UiCommand::LoadSessions {
                source: app.source(),
                action: ChatAction::Recent,
                limit: DEFAULT_SESSION_LIMIT,
                preserve_filter: false,
            }
        }
        KeyCode::Char('h') | KeyCode::Char('H') => {
            app.action_index = 1;
            UiCommand::LoadSessions {
                source: app.source(),
                action: ChatAction::Handoff,
                limit: DEFAULT_SESSION_LIMIT,
                preserve_filter: false,
            }
        }
        KeyCode::Char('n') | KeyCode::Char('N') => {
            app.action_index = 2;
            UiCommand::LoadSessions {
                source: app.source(),
                action: ChatAction::Native,
                limit: DEFAULT_SESSION_LIMIT,
                preserve_filter: false,
            }
        }
        KeyCode::Char('p') | KeyCode::Char('P') => {
            open_path_setup(app);
            UiCommand::None
        }
        KeyCode::Enter => match app.action() {
            ChatAction::Recent | ChatAction::Handoff | ChatAction::Native => {
                UiCommand::LoadSessions {
                    source: app.source(),
                    action: app.action(),
                    limit: DEFAULT_SESSION_LIMIT,
                    preserve_filter: false,
                }
            }
            ChatAction::Paths => {
                open_path_setup(app);
                UiCommand::None
            }
        },
        _ => UiCommand::None,
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
                ChatAction::Recent => {
                    let Some(detail) = app.selected_session().map(session_detail) else {
                        return UiCommand::None;
                    };
                    show_result(app, "Session Detail", detail, View::Sessions);
                    UiCommand::None
                }
                ChatAction::Handoff => {
                    app.view = View::Target;
                    app.target_index = 0;
                    UiCommand::None
                }
                ChatAction::Native => {
                    if app.selected_session().is_none() {
                        return UiCommand::None;
                    }
                    app.preview_confirm_index = 0;
                    app.view = View::SessionPreview;
                    UiCommand::None
                }
                ChatAction::Paths => UiCommand::None,
            }
        }
        _ => UiCommand::None,
    }
}

fn handle_session_preview_key(app: &mut App, key: KeyEvent) -> UiCommand {
    match key.code {
        KeyCode::Esc | KeyCode::Char('q') | KeyCode::Char('n') | KeyCode::Char('N') => {
            app.view = View::Sessions;
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
                app.view = View::Sessions;
            }
            UiCommand::None
        }
        _ => UiCommand::None,
    }
}

fn handle_target_key(app: &mut App, key: KeyEvent) -> UiCommand {
    match key.code {
        KeyCode::Esc | KeyCode::Char('q') => {
            app.view = View::Sessions;
            UiCommand::None
        }
        KeyCode::Left => {
            let target_count = Target::available_for(app.source()).len();
            app.target_index = (app.target_index + target_count - 1) % target_count;
            UiCommand::None
        }
        KeyCode::Right => {
            let target_count = Target::available_for(app.source()).len();
            app.target_index = (app.target_index + 1) % target_count;
            UiCommand::None
        }
        KeyCode::Enter => {
            let Some(session) = app.selected_session() else {
                return UiCommand::None;
            };
            if app.target() == Target::Export {
                return UiCommand::ExportSession {
                    source: app.source(),
                    session_id: session.session_id.clone(),
                };
            }
            if app.pending_action == ChatAction::Handoff {
                UiCommand::BuildHandoff {
                    source: app.source(),
                    target: app.target(),
                    session_id: session.session_id.clone(),
                }
            } else {
                app.confirm_index = 0;
                app.view = View::ConfirmNative;
                UiCommand::None
            }
        }
        _ => UiCommand::None,
    }
}

fn handle_confirm_native_key(app: &mut App, key: KeyEvent) -> UiCommand {
    match key.code {
        KeyCode::Esc | KeyCode::Char('q') | KeyCode::Char('n') | KeyCode::Char('N') => {
            app.view = View::Target;
            UiCommand::None
        }
        KeyCode::Char('y') | KeyCode::Char('Y') => {
            let Some(session) = app.selected_session() else {
                return UiCommand::None;
            };
            UiCommand::NativeImport {
                source: app.source(),
                target: app.target(),
                session_id: session.session_id.clone(),
                apply: true,
                allow_duplicate: false,
            }
        }
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
            let Some(session) = app.selected_session() else {
                return UiCommand::None;
            };
            UiCommand::NativeImport {
                source: app.source(),
                target: app.target(),
                session_id: session.session_id.clone(),
                apply: app.confirm_index == 1,
                allow_duplicate: false,
            }
        }
        _ => UiCommand::None,
    }
}

fn handle_confirm_duplicate_key(app: &mut App, key: KeyEvent) -> UiCommand {
    match key.code {
        KeyCode::Esc | KeyCode::Char('q') | KeyCode::Char('n') | KeyCode::Char('N') => {
            show_result(
                app,
                "Duplicate Cancelled",
                app.duplicate_message.clone(),
                View::Sessions,
            );
            UiCommand::None
        }
        KeyCode::Char('y') | KeyCode::Char('Y') => {
            let Some(session) = app.selected_session() else {
                return UiCommand::None;
            };
            UiCommand::NativeImport {
                source: app.source(),
                target: app.target(),
                session_id: session.session_id.clone(),
                apply: true,
                allow_duplicate: true,
            }
        }
        KeyCode::Left | KeyCode::Right => {
            app.confirm_index = 1 - app.confirm_index.min(1);
            UiCommand::None
        }
        KeyCode::Enter => {
            if app.confirm_index == 1 {
                show_result(
                    app,
                    "Duplicate Cancelled",
                    app.duplicate_message.clone(),
                    View::Sessions,
                );
                return UiCommand::None;
            }
            let Some(session) = app.selected_session() else {
                return UiCommand::None;
            };
            UiCommand::NativeImport {
                source: app.source(),
                target: app.target(),
                session_id: session.session_id.clone(),
                apply: true,
                allow_duplicate: true,
            }
        }
        _ => UiCommand::None,
    }
}

fn handle_result_key(app: &mut App, key: KeyEvent) -> UiCommand {
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
            let max_scroll = app.result_text.lines().count().saturating_sub(1) as u16;
            app.result_scroll = app.result_scroll.saturating_add(1).min(max_scroll);
            UiCommand::None
        }
        _ => UiCommand::None,
    }
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
            source,
            target,
            session_id,
            apply,
            allow_duplicate,
        } => {
            thread::spawn(move || {
                let result =
                    api_native_import(&home, source, target, &session_id, apply, allow_duplicate);
                let _ = tx.send(WorkerResult::Native(result));
            });
        }
        UiCommand::ExportSession { source, session_id } => {
            thread::spawn(move || {
                let result = api_export(&home, source, &session_id);
                let _ = tx.send(WorkerResult::Export(result));
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
        UiCommand::None | UiCommand::Quit => {
            app.worker_rx = None;
        }
    }
}

pub fn prepare_loading(app: &mut App, command: &UiCommand) {
    if app.view != View::Loading {
        app.loading_back_view = app.view;
    }
    match command {
        UiCommand::LoadSessions {
            source,
            action,
            limit,
            preserve_filter,
        } => {
            app.pending_action = *action;
            app.session_limit = *limit;
            app.preserve_filter_after_load = *preserve_filter;
            app.loading_message = format!(
                "Loading {} sessions with fast metadata path (limit {})...",
                source.label(),
                limit
            );
            app.view = View::Loading;
        }
        UiCommand::BuildHandoff { target, .. } => {
            app.loading_message = format!("Building handoff for {}...", target.label());
            app.view = View::Loading;
        }
        UiCommand::NativeImport { target, apply, .. } => {
            app.loading_message = if *apply {
                format!("Writing native import into {}...", target.label())
            } else {
                format!("Preparing dry-run for {}...", target.label())
            };
            app.view = View::Loading;
        }
        UiCommand::ExportSession { source, .. } => {
            app.loading_message = format!("Exporting {} session to a bundle...", source.label());
            app.view = View::Loading;
        }
        UiCommand::PathsDoctor => {
            app.loading_message = "Inspecting history paths...".to_string();
            app.view = View::Loading;
        }
        UiCommand::SetPath { target, .. } => {
            app.loading_message = format!("Saving {} override...", target.label_for(app.language));
            app.view = View::Loading;
        }
        UiCommand::None | UiCommand::Quit => {}
    }
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
            show_result(app, "Prompt Handoff", text, View::Sessions);
        }
        WorkerResult::Native(Ok(text)) => {
            show_result(app, "Native Import", text, View::Sessions);
        }
        WorkerResult::Export(Ok(text)) => {
            show_result(app, "Export", text, View::Sessions);
        }
        WorkerResult::Paths(Ok(text)) => {
            show_result(app, "Path Doctor", text, View::Home);
        }
        WorkerResult::PathSet(Ok(text)) => {
            app.path_input.clear();
            show_result(app, "Path Setup", text, View::PathSetup);
        }
        WorkerResult::Native(Err(error)) if error.kind == "duplicate" => {
            app.duplicate_message = error.message;
            app.duplicate_next_title = error.next_title.unwrap_or_else(|| "next copy".to_string());
            app.confirm_index = 0;
            app.view = View::ConfirmDuplicate;
        }
        WorkerResult::Sessions(Err(error)) => {
            show_result(app, "Error", error.message, View::Home);
        }
        WorkerResult::Handoff(Err(error))
        | WorkerResult::Native(Err(error))
        | WorkerResult::Export(Err(error)) => {
            show_result(app, "Error", error.message, View::Sessions);
        }
        WorkerResult::Paths(Err(error)) => {
            show_result(app, "Error", error.message, View::Home);
        }
        WorkerResult::PathSet(Err(error)) => {
            show_result(app, "Error", error.message, View::PathSetup);
        }
    }
    app.clamp_selection();
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
    source: Source,
    target: Target,
    session_id: &str,
    apply: bool,
    allow_duplicate: bool,
) -> ApiResult<String> {
    let mut args = vec![
        "api",
        "native-import",
        "--from",
        source.key(),
        "--to",
        target.key(),
        "--session",
        session_id,
        "--level",
        "full",
        if apply { "--apply" } else { "--dry-run" },
    ];
    if allow_duplicate {
        args.push("--allow-duplicate");
    }
    let data: TextData = call_api(home, &args)?;
    Ok(data.text)
}

fn api_paths(home: &PathBuf) -> ApiResult<String> {
    let data: TextData = call_api(home, &["api", "paths", "doctor"])?;
    Ok(data.text)
}

fn api_export(home: &PathBuf, source: Source, session_id: &str) -> ApiResult<String> {
    let data: TextData = call_api(
        home,
        &[
            "api",
            "export",
            "--from",
            source.key(),
            "--session",
            session_id,
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
        render_loading(frame, app, centered_rect(54, 36, area));
    }
    if app.view == View::SessionPreview {
        render_session_preview(frame, app, centered_rect(68, 42, area));
    }
    if app.view == View::ConfirmDuplicate {
        render_duplicate_confirm(frame, app, centered_rect(62, 32, area));
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

fn render_body(frame: &mut Frame<'_>, app: &mut App, area: Rect) {
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
    match app.view {
        View::Home => render_home(frame, app, area),
        View::Sessions => render_sessions(frame, app, area),
        View::SessionPreview => render_sessions(frame, app, area),
        View::Target => render_target(frame, app, area),
        View::ConfirmNative => render_confirm_native(frame, app, area),
        View::PathSetup => render_path_setup(frame, app, area),
        View::Result => render_result(frame, app, area),
        // Modals draw over the view the user came from, not always Home.
        View::ConfirmDuplicate => render_sessions(frame, app, area),
        View::Loading => match app.loading_back_view {
            View::Sessions
            | View::SessionPreview
            | View::Target
            | View::ConfirmNative
            | View::ConfirmDuplicate => render_sessions(frame, app, area),
            _ => render_home(frame, app, area),
        },
    }
}

fn render_home(frame: &mut Frame<'_>, app: &App, area: Rect) {
    let action = app.action();
    let compact = area.width < 60;
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
                Span::styled(action.label_for(app.language), value_style()),
            ]),
        ]
    } else {
        vec![
            Line::from(vec![
                Span::styled("  ChatBridge Interactive Mode", accent_style()),
                Span::styled("  AI history bridge", subtle_style()),
            ]),
            Line::from(""),
            Line::from(vec![Span::styled("  Connection Details", label_style())]),
            Line::from(vec![
                Span::styled("  Source      ", label_style()),
                Span::styled(format!(" {} ", app.source().label()), value_style()),
                Span::raw("   "),
                Span::styled("Status  ", label_style()),
                Span::styled(" ready ", success_style()),
            ]),
            Line::from(vec![
                Span::styled("  Action      ", label_style()),
                Span::styled(action.label_for(app.language), value_style()),
            ]),
            Line::from(vec![
                Span::styled("  Workflow    ", label_style()),
                Span::styled(action.description_for(app.language), subtle_style()),
            ]),
        ]
    };
    lines.push(Line::from(""));
    lines.extend(chat_bridge_logo_lines(area.width));
    frame.render_widget(
        content_block(text(app.language, "Dashboard", "控制台"), lines),
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
            ListItem::new(session_list_lines(session, visible_index, chunks[1].width))
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
    let header = format!(
        "{}  Loaded {} / {}  Limit {}{}",
        app.pending_action.label_for(app.language),
        app.session_loaded,
        app.session_total,
        app.session_limit,
        filter_text
    );
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

fn render_target(frame: &mut Frame<'_>, app: &App, area: Rect) {
    let selected = app
        .selected_session()
        .map(|session| session.title.as_str())
        .unwrap_or("-");
    let options = Target::available_for(app.source())
        .iter()
        .enumerate()
        .map(|(index, target)| {
            if index == app.target_index {
                Span::styled(format!(" {} ", target.label()), selected_style())
            } else {
                Span::styled(format!(" {} ", target.label()), inactive_chip_style())
            }
        })
        .collect::<Vec<_>>();
    let lines = vec![
        Line::from(Span::styled("Choose native target", accent_style())),
        Line::from(""),
        Line::from(format!("Session: {selected}")),
        Line::from(""),
        Line::from(options),
    ];
    frame.render_widget(content_block(" Target ", lines), area);
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
    let selected = app
        .selected_session()
        .map(|session| session.title.as_str())
        .unwrap_or("-");
    let started = app
        .selected_session()
        .map(|session| session.start_time_label())
        .unwrap_or_else(|| "-".to_string());
    let updated = app
        .selected_session()
        .map(|session| session.updated_time_label())
        .unwrap_or_else(|| "-".to_string());
    let project = app
        .selected_session()
        .map(|session| session.project_label())
        .unwrap_or_else(|| "-".to_string());
    let options = ["Dry run", "Apply", "Cancel"];
    let lines = vec![
        Line::from(Span::styled("Native Import", accent_style())),
        Line::from(""),
        Line::from(format!("Session: {selected}")),
        Line::from(format!("Started: {started}")),
        Line::from(format!("Updated: {updated}")),
        Line::from(format!("Target: {}", app.target().label())),
        Line::from(vec![
            Span::raw("Import into project: "),
            Span::styled(project, secondary_style()),
        ]),
        Line::from(""),
        button_row(&options, app.confirm_index),
        Line::from(""),
        Line::from(Span::styled(
            text(
                app.language,
                "Enter confirm  Y apply now  N/Esc cancel",
                "Enter 确认  Y 直接执行  N/Esc 取消",
            ),
            subtle_style(),
        )),
    ];
    frame.render_widget(content_block(" Confirm ", lines), area);
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

fn render_session_preview(frame: &mut Frame<'_>, app: &App, area: Rect) {
    frame.render_widget(Clear, area);
    let Some(session) = app.selected_session() else {
        frame.render_widget(
            content_block(
                text(app.language, " Chat Preview ", " 会话预览 "),
                vec![Line::from(text(
                    app.language,
                    "No session selected.",
                    "未选择会话。",
                ))],
            ),
            area,
        );
        return;
    };
    let options = [
        text(app.language, "Continue", "继续"),
        text(app.language, "Cancel", "取消"),
    ];
    let lines = vec![
        Line::from(vec![
            Span::styled(
                format!("{}: ", text(app.language, "Title", "标题")),
                label_style(),
            ),
            Span::raw(session.title.clone()),
        ]),
        Line::from(vec![
            Span::styled(
                format!("{}: ", text(app.language, "Project", "项目")),
                label_style(),
            ),
            Span::raw(session.project_label()),
        ]),
        Line::from(vec![
            Span::styled(
                format!("{}: ", text(app.language, "Started", "开始")),
                label_style(),
            ),
            Span::raw(session.start_time_label()),
        ]),
        Line::from(vec![
            Span::styled(
                format!("{}: ", text(app.language, "Updated", "更新")),
                label_style(),
            ),
            Span::raw(session.updated_time_label()),
        ]),
        Line::from(vec![
            Span::styled(
                format!("{}: ", text(app.language, "Source", "来源")),
                label_style(),
            ),
            Span::raw(session.source_label.clone()),
            Span::styled(
                format!("  {}: ", text(app.language, "Scope", "范围")),
                label_style(),
            ),
            Span::raw(session.scope_label()),
        ]),
        Line::from(vec![
            Span::styled("Session ID: ", label_style()),
            Span::raw(session.session_id.clone()),
        ]),
        Line::from(""),
        button_row(&options, app.preview_confirm_index),
        Line::from(""),
        Line::from(Span::styled(
            text(
                app.language,
                "Enter confirm  Y continue  N/Esc cancel",
                "Enter 确认  Y 继续  N/Esc 取消",
            ),
            subtle_style(),
        )),
    ];
    frame.render_widget(
        content_block(text(app.language, " Chat Preview ", " 会话预览 "), lines),
        area,
    );
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
    let footer = match app.view {
        View::Home => text(
            app.language,
            "NAV  Tab source  Up/Down action  Enter select  T language    ACT  L recent  H handoff  N import  P paths  q quit",
            "导航  Tab 来源  上/下 操作  Enter 选择  T 语言    操作  L 最近  H 接续  N 导入  P 路径  q 退出",
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
            "NAV  Left/Right choice  Enter confirm  Y apply  N/Esc cancel  T language",
            "导航  左/右 选择  Enter 确认  Y 执行  N/Esc 取消  T 语言",
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
            "NAV  Up/Down scroll  Esc/q back  T language",
            "导航  上/下 滚动  Esc/q 返回  T 语言",
        ),
        View::Loading => text(
            app.language,
            "Loading...  Esc cancel  Ctrl+C quit",
            "加载中...  Esc 取消  Ctrl+C 退出",
        ),
    };
    let footer = format!("{footer}    LANG {}    v{VERSION}", app.language.label());
    frame.render_widget(
        Paragraph::new(footer).style(subtle_style()).block(
            Block::default()
                .borders(Borders::TOP)
                .border_style(border_style()),
        ),
        area,
    );
}

fn render_loading(frame: &mut Frame<'_>, app: &App, area: Rect) {
    let frames = ["-", "\\", "|", "/"];
    let spinner = frames[app.tick % frames.len()];
    let bar_width = usize::from(area.width.saturating_sub(10)).clamp(12, 34);
    let progress = loading_progress_bar(app.tick, bar_width);
    frame.render_widget(Clear, area);
    let text = vec![
        Line::from(Span::styled(format!("{spinner} Working"), accent_style())),
        Line::from(""),
        Line::from(app.loading_message.clone()),
        Line::from(""),
        Line::from(Span::styled(progress, secondary_style())),
        Line::from(Span::styled(
            "Fast metadata reads avoid fully loading large chat transcripts.",
            subtle_style(),
        )),
    ];
    frame.render_widget(content_block(" Loading ", text), area);
}

fn render_duplicate_confirm(frame: &mut Frame<'_>, app: &App, area: Rect) {
    frame.render_widget(Clear, area);
    let options = ["Import duplicate", "Cancel"];
    let session_title = app
        .selected_session()
        .map(|session| session.title.clone())
        .unwrap_or_else(|| "-".to_string());
    let text_lines = vec![
        Line::from(Span::styled("Duplicate native import", accent_style())),
        Line::from(""),
        Line::from(format!("Session: {session_title}")),
        Line::from(app.duplicate_message.clone()),
        Line::from(""),
        Line::from(format!("Next title: {}", app.duplicate_next_title)),
        Line::from(""),
        button_row(&options, app.confirm_index),
        Line::from(""),
        Line::from(Span::styled(
            text(
                app.language,
                "Enter confirm  Y import  N/Esc cancel",
                "Enter 确认  Y 导入  N/Esc 取消",
            ),
            subtle_style(),
        )),
    ];
    frame.render_widget(content_block(" Confirm Duplicate ", text_lines), area);
}

fn render_filter(frame: &mut Frame<'_>, app: &App, area: Rect) {
    frame.render_widget(Clear, area);
    let text = vec![
        Line::from("Search title / project path / session id. Enter applies, Esc cancels."),
        Line::from(""),
        Line::from(Span::styled(
            format!(" /{} ", app.filter_input),
            selected_style(),
        )),
    ];
    frame.render_widget(content_block(" Filter ", text), area);
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
) -> Vec<Line<'static>> {
    let project = session.project_label();
    let available = usize::from(width).saturating_sub(8).max(30);
    let project_width = available.saturating_div(3).clamp(18, 52);
    let title_width = available.saturating_sub(project_width + 4).max(18);
    let id_width = usize::from(width).saturating_sub(28).clamp(24, 72);
    vec![
        Line::from(vec![
            Span::styled(format!("{:>2}. ", visible_index + 1), muted_style()),
            Span::raw(compact_title(&session.title, title_width)),
            Span::raw("  "),
            Span::styled(compact_title(&project, project_width), subtle_style()),
        ]),
        Line::from(vec![
            Span::raw("    "),
            Span::styled(
                format!("{:<7}", compact_title(&session.scope_label(), 7)),
                secondary_style(),
            ),
            Span::raw("  "),
            Span::styled(
                format!("Started: {}", session.start_time_label()),
                subtle_style(),
            ),
            Span::raw("  "),
            Span::styled(
                format!("ID: {}", compact_title(&session.session_id, id_width)),
                subtle_style(),
            ),
            Span::raw("  "),
            Span::styled(
                format!("Updated: {}", session.updated_time_label()),
                subtle_style(),
            ),
        ]),
    ]
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

        assert!(matches!(command, UiCommand::None));
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

        let _ = handle_key(&mut app, key(KeyCode::Enter));
        assert_eq!(app.view(), &View::Result);

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
        app.action_index = 3;

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
