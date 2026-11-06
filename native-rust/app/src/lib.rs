use serde::{Deserialize, Serialize};
use std::{collections::HashSet, path::{Path, PathBuf}};

#[derive(Clone, Serialize, Deserialize)]
pub struct Track { pub path: PathBuf, pub title: String, pub seconds: Option<f64> }
#[derive(Clone, Serialize, Deserialize)]
pub struct Playlist { pub name: String, pub paths: Vec<PathBuf> }
#[derive(Clone, Copy, Default, PartialEq, Eq, Serialize, Deserialize)]
pub enum Repeat { #[default] Off, All, One }
#[derive(Serialize, Deserialize)]
pub struct Session {
    pub schema_version: u32,
    pub library: Vec<Track>, pub playlists: Vec<Playlist>, pub queue: Vec<PathBuf>,
    pub cursor: Option<usize>, pub position: f64, pub volume: f32, pub repeat: Repeat,
    pub ffmpeg: PathBuf,
}
impl Default for Session {
    fn default() -> Self { Self { schema_version:1, library:vec![],playlists:vec![],queue:vec![],cursor:None,position:0.0,volume:0.7,repeat:Repeat::Off,
        ffmpeg:PathBuf::from(r"D:\Projects\MusicStudio-Rust\tools\ffmpeg\ffmpeg.exe") } }
}
impl Session {
    pub fn validate(&mut self) -> Result<(), String> {
        if self.schema_version != 1 { return Err("Unsupported Music Studio session version.".into()); }
        if self.library.len()>20_000 || self.queue.len()>20_000 || self.playlists.len()>200 || self.playlists.iter().any(|list|list.paths.len()>20_000) { return Err("Saved library exceeds this preview's limits.".into()); }
        if self.cursor.is_some_and(|index|index>=self.queue.len()) { self.cursor=None; }
        if !self.position.is_finite() || !(0.0..=31_536_000.0).contains(&self.position) { self.position=0.0; }
        if !self.volume.is_finite() { self.volume=0.7; } self.volume=self.volume.clamp(0.0,1.0);
        Ok(())
    }
    pub fn add_tracks(&mut self, tracks: Vec<Track>) -> usize {
        let mut seen: HashSet<_> = self.library.iter().map(|track|key(&track.path)).collect();
        let before=self.library.len();
        for track in tracks { if self.library.len()>=20_000 { break; } if seen.insert(key(&track.path)) { self.library.push(track); } }
        self.library.len()-before
    }
}
fn key(path:&Path)->String { path.to_string_lossy().replace('/',"\\").to_lowercase() }
pub fn audio_extension(path: &Path) -> bool {
    path.extension().and_then(|s|s.to_str()).is_some_and(|ext|["mp3","flac","wav","wave","ogg","oga","m4a","aac","aiff","aif","mp4"].contains(&ext.to_ascii_lowercase().as_str()))
}
pub fn next_index(current: usize, count: usize, repeat: Repeat, manual: bool) -> Option<usize> {
    if count==0 || current>=count { return None; }
    if repeat==Repeat::One && !manual { return Some(current); }
    if current+1<count { Some(current+1) } else if repeat==Repeat::All { Some(0) } else { None }
}
pub fn playlist_text(paths: &[PathBuf]) -> Result<String, String> {
    let mut text=String::from("#EXTM3U\r\n");
    for path in paths {
        let value=path.to_str().ok_or("Playlist paths must be valid Unicode.")?;
        if !path.is_absolute() || value.contains(['\r','\n']) || value.starts_with('#') { return Err("Playlist contains an invalid path.".into()); }
        // Windows canonicalization adds extended-path prefixes that many music players do not accept.
        let portable=if let Some(rest)=value.strip_prefix(r"\\?\UNC\") {format!(r"\\{rest}")}
            else {value.strip_prefix(r"\\?\").unwrap_or(value).to_owned()};
        text.push_str(&portable);text.push_str("\r\n");
    } Ok(text)
}

#[derive(Clone,Copy,PartialEq,Eq)]
pub enum ExportFormat { Flac, Mp3 }
impl ExportFormat { pub fn extension(self)->&'static str { if self==Self::Flac {"flac"} else {"mp3"} } }
pub fn export_args(source:&Path, destination:&Path, format:ExportFormat, start:f64, end:Option<f64>) -> Result<Vec<std::ffi::OsString>,String> {
    if !start.is_finite() || start<0.0 || end.is_some_and(|end|!end.is_finite() || end<=start) { return Err("Enter a nonnegative start and an end after the start.".into()); }
    if key(source)==key(destination) { return Err("Export must use a new file, not the source.".into()); }
    let mut args: Vec<std::ffi::OsString>=["-nostdin","-hide_banner","-loglevel","error","-n","-i"].into_iter().map(Into::into).collect();
    args.push(source.as_os_str().into());
    args.extend(["-ss".into(),format!("{start:.6}").into()]);
    if let Some(end)=end { args.extend(["-t".into(),format!("{:.6}",end-start).into()]); }
    args.extend(["-map","0:a:0","-vn","-map_metadata","0","-c:a"].into_iter().map(Into::into));
    match format {
        ExportFormat::Flac=>args.extend(["flac","-compression_level","8"].into_iter().map(Into::into)),
        ExportFormat::Mp3=>args.extend(["libmp3lame","-q:a","0"].into_iter().map(Into::into)),
    }
    args.push(destination.as_os_str().into());Ok(args)
}

#[cfg(test)] mod tests {
    use super::*;
    #[test] fn queue_ends_wraps_and_repeats_only_when_requested() {
        assert_eq!(next_index(1,2,Repeat::Off,false),None);
        assert_eq!(next_index(1,2,Repeat::All,false),Some(0));
        assert_eq!(next_index(0,2,Repeat::One,false),Some(0));
        assert_eq!(next_index(0,2,Repeat::One,true),Some(1));
        assert_eq!(next_index(0,0,Repeat::All,true),None);
    }
    #[test] fn imports_deduplicate_windows_case_without_removing_missing_tracks() {
        let mut session=Session::default();
        let track=|path:&str|Track {path:path.into(),title:"song".into(),seconds:None};
        assert_eq!(session.add_tracks(vec![track(r"D:\Music\A.mp3"),track(r"d:\music\a.mp3")]),1);
        assert_eq!(session.library.len(),1);
    }
    #[test] fn export_uses_separate_arguments_and_never_overwrites() {
        let args=export_args(Path::new(r"D:\songs\a & b.wav"),Path::new(r"D:\exports\new.flac"),ExportFormat::Flac,2.0,Some(5.5)).unwrap();
        assert!(args.contains(&"-n".into())); assert!(args.contains(&"3.500000".into()));
        assert!(args.contains(&std::ffi::OsString::from(r"D:\songs\a & b.wav")));
        assert!(!args.contains(&"-ar".into()));
    }
    #[test] fn invalid_edits_and_source_overwrites_fail() {
        let path=Path::new("song.flac");
        assert!(export_args(path,path,ExportFormat::Flac,0.0,None).is_err());
        assert!(export_args(path,Path::new("out.flac"),ExportFormat::Flac,4.0,Some(2.0)).is_err());
        assert!(export_args(path,Path::new("out.flac"),ExportFormat::Flac,f64::NAN,None).is_err());
    }
    #[test] fn playlist_rejects_injected_lines() {
        assert!(playlist_text(&[PathBuf::from("D:\\song\n#EXTINF:bad")]).is_err());
        assert!(playlist_text(&[PathBuf::from("relative.mp3")]).is_err());
    }
    #[test] #[cfg(windows)] fn playlist_exports_portable_windows_paths() {
        assert_eq!(playlist_text(&[PathBuf::from(r"\\?\D:\Music\song.mp3")]).unwrap(),"#EXTM3U\r\nD:\\Music\\song.mp3\r\n");
    }
}
