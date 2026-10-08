//! Append-only journal compatible with the existing Python live runner.
use anyhow::{Context, Result, ensure};
use serde_json::{Value, json};
use std::{
    collections::HashSet,
    fs::{File, OpenOptions},
    io::{BufRead, BufReader, Write},
    path::Path,
};

pub struct Journal {
    _lock: File,
    file: File,
    pub rows: Vec<Value>,
    pub windows: HashSet<i64>,
    pub waiting: Vec<Value>,
}

pub struct AccountLocks {
    _files: Vec<File>,
}

impl AccountLocks {
    pub fn acquire(accounts: &[(&str, alloy_primitives::Address)]) -> Result<Self> {
        let home = if cfg!(windows) {
            std::env::var_os("LOCALAPPDATA")
                .map(std::path::PathBuf::from)
                .or_else(|| {
                    std::env::var_os("USERPROFILE")
                        .map(|home| std::path::PathBuf::from(home).join("AppData/Local"))
                })
        } else {
            std::env::var_os("XDG_CACHE_HOME")
                .map(std::path::PathBuf::from)
                .or_else(|| {
                    std::env::var_os("HOME")
                        .map(|home| std::path::PathBuf::from(home).join(".cache"))
                })
        }
        .context("Account lock directory unavailable")?;
        let directory = home.join("prediction-market-arbitrage/account-locks");
        std::fs::create_dir_all(&directory)?;
        let mut names: Vec<String> = accounts
            .iter()
            .map(|(venue, address)| format!("{venue}-{address:#x}.lock"))
            .collect();
        names.sort();
        names.dedup();
        let mut files = Vec::new();
        for name in names {
            let mut file = OpenOptions::new()
                .read(true)
                .write(true)
                .create(true)
                .truncate(false)
                .open(directory.join(name))?;
            if file.metadata()?.len() == 0 {
                file.write_all(b"0")?;
                file.sync_all()?;
            }
            file.try_lock()
                .context("Another process uses a live account on this host")?;
            files.push(file);
        }
        Ok(Self { _files: files })
    }
}

impl Journal {
    pub fn open(path: &Path) -> Result<Self> {
        if let Some(parent) = path.parent().filter(|p| !p.as_os_str().is_empty()) {
            std::fs::create_dir_all(parent)?;
        }
        let mut lock = OpenOptions::new()
            .read(true)
            .write(true)
            .create(true)
            .truncate(false)
            .open(path.with_extension("lock"))?;
        if lock.metadata()?.len() == 0 {
            lock.write_all(b"0")?;
            lock.sync_all()?;
        }
        // Windows locks overlap Python's byte-zero msvcrt lock; Unix uses flock.
        lock.try_lock()
            .context("Another process owns this live journal")?;
        let mut rows = Vec::new();
        if path.exists() {
            let mut reader = BufReader::new(File::open(path)?);
            let mut line = String::new();
            while reader.read_line(&mut line)? != 0 {
                ensure!(
                    line.ends_with('\n'),
                    "Truncated live journal; reconcile without clearing it"
                );
                rows.push(
                    serde_json::from_str::<Value>(&line)
                        .context("Invalid live journal; preserve it for reconciliation")?,
                );
                line.clear();
            }
        }
        let finished: HashSet<&str> = rows
            .iter()
            .filter(|r| r["type"] == "DONE" || r["type"] == "RECONCILED")
            .filter_map(|r| r["attempt"].as_str())
            .collect();
        let reconciled: HashSet<&str> = rows
            .iter()
            .filter(|r| r["type"] == "RECONCILED")
            .filter_map(|r| r["attempt"].as_str())
            .collect();
        let mut waiting = Vec::new();
        let mut waiting_ids = HashSet::new();
        for row in rows
            .iter()
            .rev()
            .filter(|r| r["type"] == "AWAIT_SETTLEMENT" || r["type"] == "AWAIT_CONFIRMATION")
        {
            let id = row["attempt"].as_str().context("Journal attempt missing")?;
            if !finished.contains(id) && waiting_ids.insert(id) {
                waiting.push(row.clone());
            }
        }
        for row in &rows {
            let id = row["attempt"].as_str().context("Journal attempt missing")?;
            ensure!(
                row["type"] != "HALT" || reconciled.contains(id),
                "Halted live journal; reconcile before starting again"
            );
            ensure!(
                row["type"] != "INTENT" || finished.contains(id) || waiting_ids.contains(id),
                "Unresolved live journal; reconcile before starting again"
            );
        }
        let windows = rows
            .iter()
            .filter(|r| r["type"] == "INTENT")
            .filter_map(|r| r["window_start"].as_i64())
            .collect();
        let file = OpenOptions::new().append(true).create(true).open(path)?;
        Ok(Self {
            _lock: lock,
            file,
            rows,
            windows,
            waiting,
        })
    }

    pub fn write(&mut self, kind: &str, attempt: &str, fields: Value) -> Result<Value> {
        let mut row = json!({"type": kind, "attempt": attempt, "recorded_at_ms": crate::now_ms()});
        for (key, value) in fields.as_object().context("Invalid journal fields")? {
            ensure!(
                !["type", "attempt", "recorded_at_ms"].contains(&key.as_str()),
                "Reserved journal field"
            );
            row[key] = value.clone();
        }
        let mut bytes = serde_json::to_vec(&row)?;
        bytes.push(b'\n');
        self.file.write_all(&bytes)?;
        self.file.sync_all()?;
        self.rows.push(row.clone());
        log::info!("LIVE {}", row);
        Ok(row)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn unresolved_journal_is_preserved_and_lock_is_exclusive() {
        let path = std::env::temp_dir().join(format!("prediction-{}.jsonl", uuid::Uuid::new_v4()));
        let mut first = Journal::open(&path).unwrap();
        assert!(Journal::open(&path).is_err());
        first
            .write("INTENT", "one", json!({"window_start": 600}))
            .unwrap();
        drop(first);
        let before = std::fs::read(&path).unwrap();
        assert!(Journal::open(&path).is_err());
        assert_eq!(std::fs::read(&path).unwrap(), before);
        std::fs::remove_file(&path).unwrap();
        std::fs::remove_file(path.with_extension("lock")).unwrap();
    }
    #[test]
    fn resumes_confirmation_without_resubmitting() {
        let path = std::env::temp_dir().join(format!("prediction-{}.jsonl", uuid::Uuid::new_v4()));
        let mut journal = Journal::open(&path).unwrap();
        journal
            .write("INTENT", "one", json!({"window_start": 600}))
            .unwrap();
        journal
            .write("AWAIT_CONFIRMATION", "one", json!({"new_entries": false}))
            .unwrap();
        drop(journal);
        let journal = Journal::open(&path).unwrap();
        assert_eq!(journal.waiting.len(), 1);
        assert!(journal.windows.contains(&600));
        drop(journal);
        std::fs::remove_file(&path).unwrap();
        std::fs::remove_file(path.with_extension("lock")).unwrap();
    }

    #[test]
    fn python_and_rust_journal_locks_overlap() {
        use std::{
            io::{BufRead, BufReader},
            process::{Command, Stdio},
        };
        let root = std::path::Path::new(env!("CARGO_MANIFEST_DIR"));
        let python = root.join(if cfg!(windows) {
            "python/.venv/Scripts/python.exe"
        } else {
            "python/.venv/bin/python"
        });
        if !python.exists() {
            return;
        }
        let path = std::env::temp_dir().join(format!("prediction-{}.jsonl", uuid::Uuid::new_v4()));
        let code = "import os,sys; f=open(sys.argv[1],'a+b'); f.write(b'0'); f.flush(); f.seek(0); exec('import msvcrt; msvcrt.locking(f.fileno(),msvcrt.LK_NBLCK,1)' if os.name=='nt' else 'import fcntl; fcntl.flock(f.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)'); print('locked',flush=True); sys.stdin.readline()";
        let mut child = Command::new(python)
            .args(["-c", code])
            .arg(path.with_extension("lock"))
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .spawn()
            .unwrap();
        let mut output = BufReader::new(child.stdout.take().unwrap());
        let mut line = String::new();
        output.read_line(&mut line).unwrap();
        assert_eq!(line.trim(), "locked");
        let blocked = Journal::open(&path).is_err();
        child.stdin.take().unwrap().write_all(b"stop\n").unwrap();
        child.wait().unwrap();
        assert!(blocked);
        let journal = Journal::open(&path).unwrap();
        drop(journal);
        std::fs::remove_file(&path).unwrap();
        std::fs::remove_file(path.with_extension("lock")).unwrap();
    }

    #[test]
    fn truncated_records_and_halts_are_never_cleared() {
        let path = std::env::temp_dir().join(format!("prediction-{}.jsonl", uuid::Uuid::new_v4()));
        let bytes = b"{\"type\":\"INTENT\",\"attempt\":\"a\"}";
        std::fs::write(&path, bytes).unwrap();
        assert!(Journal::open(&path).is_err());
        assert_eq!(std::fs::read(&path).unwrap(), bytes);
        std::fs::write(
            &path,
            b"{\"type\":\"HALT\",\"attempt\":\"a\"}\n{\"type\":\"DONE\",\"attempt\":\"a\"}\n",
        )
        .unwrap();
        assert!(Journal::open(&path).is_err());
        std::fs::remove_file(&path).unwrap();
        std::fs::remove_file(path.with_extension("lock")).unwrap();
    }
}
