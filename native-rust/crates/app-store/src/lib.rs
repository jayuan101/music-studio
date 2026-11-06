//! App-local versioned JSON snapshots. No other application's data is opened.
use serde::{de::DeserializeOwned, Serialize};
use std::{fs::{self, File, OpenOptions}, io::{Read, Write}, path::{Path, PathBuf}, sync::atomic::{AtomicU64, Ordering}, time::{SystemTime, UNIX_EPOCH}};
pub type Result<T> = std::result::Result<T,String>;
const MAX_JSON: u64 = 32 * 1024 * 1024;
static COUNTER: AtomicU64 = AtomicU64::new(0);
pub fn read_json<T: DeserializeOwned>(path: &Path) -> Result<T> {
    let file = File::open(path).map_err(|error| format!("Cannot open {}: {error}", path.display()))?;
    let mut bytes = Vec::new();
    file.take(MAX_JSON + 1).read_to_end(&mut bytes).map_err(|_| "Could not read saved data.")?;
    if bytes.len() as u64 > MAX_JSON { return Err("Saved data exceeds the 32 MiB limit.".into()); }
    serde_json::from_slice(&bytes).map_err(|_| "Saved data is not valid JSON for this application.".into())
}

pub fn write_new(path: &Path, content: &[u8]) -> Result<()> {
    let mut file = OpenOptions::new().write(true).create_new(true).open(path)
        .map_err(|error| format!("Cannot create {} without replacing another file: {error}", path.display()))?;
    file.write_all(content).and_then(|_| file.sync_all())
        .map_err(|_| format!("Write failed. A partial file may remain at {}.", path.display()))
}

pub fn save_snapshot<T: Serialize>(directory: &Path, value: &T) -> Result<PathBuf> {
    let content = serde_json::to_vec_pretty(value).map_err(|_| "Cannot encode saved data.")?;
    if content.len() as u64 > MAX_JSON { return Err("Saved data exceeds the 32 MiB limit.".into()); }
    fs::create_dir_all(directory).map_err(|_| "Cannot create the data folder on D:.")?;
    let time = SystemTime::now().duration_since(UNIX_EPOCH).map_err(|_| "System clock is before 1970.")?.as_nanos();
    let serial = COUNTER.fetch_add(1, Ordering::Relaxed);
    let path = directory.join(format!("{time:025}-{}-{serial:08}.json", std::process::id()));
    write_new(&path, &content)?;
    Ok(path)
}

pub fn latest_snapshot<T: DeserializeOwned>(directory: &Path) -> Result<Option<T>> {
    if !directory.exists() { return Ok(None); }
    let mut paths = fs::read_dir(directory).map_err(|_| "Cannot read settings snapshots.")?
        .filter_map(|entry| entry.ok()).map(|entry| entry.path())
        .filter(|path| path.is_file() && path.extension().and_then(|extension| extension.to_str()) == Some("json"))
        .collect::<Vec<_>>();
    paths.sort();
    match paths.last() { Some(path) => read_json(path).map(Some), None => Ok(None) }
}

#[cfg(test)] mod tests {
    use super::*;
    #[test] fn snapshot_round_trip_retains_previous_versions() {
        let directory=std::env::temp_dir().join(format!("app-store-{}-{}",std::process::id(),SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_nanos()));
        let first=save_snapshot(&directory,&vec!["first"]).unwrap();
        let second=save_snapshot(&directory,&vec!["second"]).unwrap();
        assert_ne!(first,second);assert_eq!(read_json::<Vec<String>>(&first).unwrap(),vec!["first"]);
        assert_eq!(latest_snapshot::<Vec<String>>(&directory).unwrap().unwrap(),vec!["second"]);
        assert!(write_new(&first,b"replacement").is_err());
        fs::remove_file(first).unwrap();fs::remove_file(second).unwrap();fs::remove_dir(directory).unwrap();
    }
}
