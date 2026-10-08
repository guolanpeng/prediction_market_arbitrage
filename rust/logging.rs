//! Append-only local diagnostics; SDK log targets are deliberately excluded.
use anyhow::Result;
use std::{
    fs::{File, OpenOptions},
    io::Write,
    path::Path,
    sync::Mutex,
};

struct Logger {
    file: Mutex<File>,
}
impl log::Log for Logger {
    fn enabled(&self, metadata: &log::Metadata<'_>) -> bool {
        metadata.level() <= log::Level::Info && metadata.target().starts_with("prediction_market")
    }
    fn log(&self, record: &log::Record<'_>) {
        if !self.enabled(record.metadata()) {
            return;
        }
        let line = format!(
            "{} {} {}\n",
            chrono::Local::now().format("%Y-%m-%d %H:%M:%S"),
            record.level(),
            record.args()
        );
        eprint!("{line}");
        if let Ok(mut file) = self.file.lock() {
            let _ = file.write_all(line.as_bytes());
            let _ = file.flush();
        }
    }
    fn flush(&self) {
        if let Ok(mut file) = self.file.lock() {
            let _ = file.flush();
        }
    }
}
pub fn init(live: bool) -> Result<()> {
    let directory = Path::new("logs/prediction");
    std::fs::create_dir_all(directory)?;
    let file = OpenOptions::new()
        .append(true)
        .create(true)
        .open(directory.join(format!(
            "{}-{}.log",
            if live { "live" } else { "check" },
            chrono::Local::now().format("%Y-%m-%d")
        )))?;
    log::set_boxed_logger(Box::new(Logger {
        file: Mutex::new(file),
    }))
    .map_err(|_| anyhow::anyhow!("Logger initialization failed"))?;
    log::set_max_level(log::LevelFilter::Info);
    log::info!("RUST_BTC_RUNNER");
    Ok(())
}
