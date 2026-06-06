fn main() {
    if let Err(error) = chatbridge_tui::run() {
        eprintln!("chatbridge-tui error: {error:#}");
        std::process::exit(1);
    }
}
