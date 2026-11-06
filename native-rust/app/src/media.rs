use music_studio_native::{audio_extension, render_args, Edit, ExportFormat, SourceAudio, Track};
use rodio::{Decoder, Source};
use std::{fs::{self,File,OpenOptions},path::{Path,PathBuf},time::{Duration,Instant,SystemTime,UNIX_EPOCH}};

pub fn scan(inputs: Vec<PathBuf>) -> Result<(Vec<Track>,usize),String> {
    let mut pending=inputs;let mut tracks=vec![];let mut skipped=0;let mut inspected=0;
    while let Some(path)=pending.pop() {
        inspected+=1;
        if inspected>100_000 || tracks.len()>=20_000 { return Err("Import is too large. Choose a smaller folder (up to 20,000 tracks).".into()); }
        let metadata=match fs::symlink_metadata(&path) {Ok(metadata)=>metadata,Err(_)=>{skipped+=1;continue;}};
        #[cfg(windows)] { use std::os::windows::fs::MetadataExt; if metadata.file_attributes() & 0x400 != 0 {skipped+=1;continue;} }
        if metadata.file_type().is_symlink() {skipped+=1;continue;}
        if metadata.is_dir() {
            match fs::read_dir(&path) {Ok(entries)=>for entry in entries {match entry {Ok(entry)=>pending.push(entry.path()),Err(_)=>skipped+=1}},Err(_)=>skipped+=1}
            continue;
        }
        if !audio_extension(&path) {continue;}
        match File::open(&path).ok().and_then(|file|Decoder::try_from(file).ok()) {
            Some(decoder)=> {
                let seconds=decoder.total_duration().map(|duration|duration.as_secs_f64());
                let title=path.file_stem().unwrap_or_default().to_string_lossy().into_owned();
                let path=fs::canonicalize(path).map_err(|error|error.to_string())?;
                tracks.push(Track {path,title,seconds});
            },None=>skipped+=1,
        }
    }
    tracks.sort_by_cached_key(|track|track.title.to_lowercase());Ok((tracks,skipped))
}

fn probe(ffprobe:&Path,source:&Path)->Result<SourceAudio,String> {
    if !source.is_file(){return Err("Source audio file is missing.".into());}
    if !ffprobe.is_file(){return Err("Keep ffprobe.exe beside the selected ffmpeg.exe so source quality can be checked.".into());}
    let mut command=std::process::Command::new(ffprobe);
    command.args(["-v","error","-select_streams","a:0","-show_entries","stream=codec_name,sample_rate,channels,bits_per_sample,bits_per_raw_sample,sample_fmt,duration:format=duration","-of","json","-protocol_whitelist","file","-i"])
        .arg(source).stdin(std::process::Stdio::null()).stdout(std::process::Stdio::piped()).stderr(std::process::Stdio::null());
    #[cfg(windows)] {use std::os::windows::process::CommandExt;command.creation_flags(0x08000000);}
    let mut child=command.spawn().map_err(|error|format!("Could not inspect source audio: {error}"))?;
    let begin=Instant::now();
    loop {
        match child.try_wait(){
            Ok(Some(_))=>break,
            Ok(None)=>{},
            Err(error)=>{let _=child.kill();let _=child.wait();return Err(error.to_string());}
        }
        if begin.elapsed()>Duration::from_secs(30){let _=child.kill();let _=child.wait();return Err("Source inspection timed out after 30 seconds.".into());}
        std::thread::sleep(Duration::from_millis(50));
    }
    let output=child.wait_with_output().map_err(|error|error.to_string())?;
    if !output.status.success(){return Err("FFprobe could not read this local audio source.".into());}
    let value:serde_json::Value=serde_json::from_slice(&output.stdout).map_err(|_|"Invalid source inspection response.")?;
    let stream=value.pointer("/streams/0").ok_or("No audio stream found.")?;
    let number=|key:&str|stream.get(key).and_then(|v|v.as_u64().or_else(||v.as_str()?.parse().ok())).and_then(|n|u32::try_from(n).ok());
    let sample=stream.get("sample_fmt").and_then(|v|v.as_str()).unwrap_or("");
    let codec=stream.get("codec_name").and_then(|v|v.as_str()).unwrap_or("");
    // Lossy decoders also output float; that is not the source's stored precision.
    let float=(sample.starts_with('f')||sample.starts_with('d'))&&(codec.starts_with("pcm_")||codec=="wavpack");
    let bits=number("bits_per_raw_sample").filter(|n|*n>0).or_else(||number("bits_per_sample").filter(|n|*n>0))
        .or(if float {Some(if sample.starts_with('d'){64}else{32})}else{None});
    let duration_value=|v:Option<&serde_json::Value>|v.and_then(|v|v.as_str()?.parse::<f64>().ok()).filter(|n|n.is_finite()&&*n>0.0);
    let duration=duration_value(stream.get("duration")).or_else(||duration_value(value.pointer("/format/duration")));
    Ok(SourceAudio {rate:number("sample_rate").ok_or("Source sample rate is missing.")?,channels:number("channels").ok_or("Source channel count is missing.")?,bits,float,duration})
}

pub fn export(ffmpeg:&Path, source:&Path, root:&Path, format:ExportFormat, edit:&Edit)->Result<PathBuf,String> {
    if !ffmpeg.is_file() {return Err("Choose the existing ffmpeg.exe in the export panel.".into());}
    fs::create_dir_all(root.join("exports")).map_err(|error|error.to_string())?;
    fs::create_dir_all(root.join("logs")).map_err(|error|error.to_string())?;
    let stamp=SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default().as_nanos();
    let destination=root.join("exports").join(format!("export-{stamp}.{}",format.extension()));
    if destination.exists() {return Err("Export path already exists. Retry to create a new filename.".into());}
    let audio=probe(&ffmpeg.with_file_name("ffprobe.exe"),source)?;
    let args=render_args(source,&destination,format,&audio,edit)?;
    let log_path=root.join("logs").join(format!("export-{stamp}.txt"));
    let log=OpenOptions::new().write(true).create_new(true).open(&log_path).map_err(|error|error.to_string())?;
    let mut command=std::process::Command::new(ffmpeg);
    command.args(args).stdin(std::process::Stdio::null()).stdout(std::process::Stdio::null()).stderr(log);
    #[cfg(windows)] {use std::os::windows::process::CommandExt;command.creation_flags(0x08000000);}
    let mut child=command.spawn().map_err(|error|format!("Could not start FFmpeg: {error}"))?;
    let begin=Instant::now();
    loop {
        match child.try_wait() {
            Ok(Some(status))=> {
                // Some FFmpeg versions return zero even when -n refuses an output.
                let output_ok=fs::metadata(&destination).is_ok_and(|meta|meta.is_file()&&meta.len()>0);
                let log_clean=fs::metadata(&log_path).is_ok_and(|meta|meta.len()==0);
                return if status.success()&&output_ok&&log_clean {Ok(destination)} else {Err(format!("Export failed or reported errors. Source unchanged; any partial export was kept. Details: {}",log_path.display()))};
            },
            Ok(None)=>{},
            Err(error)=>{let _=child.kill();let _=child.wait();return Err(error.to_string());}
        }
        if begin.elapsed()>Duration::from_secs(1800) {let _=child.kill();let _=child.wait();return Err(format!("Export stopped after 30 minutes. Source unchanged; a partial export may remain at {}.",destination.display()));}
        std::thread::sleep(Duration::from_millis(100));
    }
}

#[cfg(test)] mod tests {
    use super::*;
    #[test] fn scans_and_decodes_real_pcm_without_an_audio_device() {
        let stamp=SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_nanos();
        let directory=std::env::temp_dir().join(format!("music-rust-test-{}-{stamp}",std::process::id()));
        fs::create_dir(&directory).unwrap();
        let audio=directory.join("tone.wav");let invalid=directory.join("bad.mp3");let note=directory.join("note.txt");
        let data_len=1600u32;let mut bytes=vec![];
        bytes.extend(b"RIFF");bytes.extend((36+data_len).to_le_bytes());bytes.extend(b"WAVEfmt ");
        bytes.extend(16u32.to_le_bytes());bytes.extend(1u16.to_le_bytes());bytes.extend(1u16.to_le_bytes());
        bytes.extend(8000u32.to_le_bytes());bytes.extend(16000u32.to_le_bytes());bytes.extend(2u16.to_le_bytes());bytes.extend(16u16.to_le_bytes());
        bytes.extend(b"data");bytes.extend(data_len.to_le_bytes());bytes.resize(44+data_len as usize,0);
        fs::write(&audio,bytes).unwrap();fs::write(&invalid,b"invalid audio").unwrap();fs::write(&note,b"ignore").unwrap();
        let (tracks,skipped)=scan(vec![directory.clone()]).unwrap();
        assert_eq!(tracks.len(),1);assert_eq!(tracks[0].title,"tone");assert_eq!(skipped,1);
        let decoder=Decoder::try_from(File::open(&audio).unwrap()).unwrap();assert_eq!(decoder.count(),800);
        for path in [&audio,&invalid,&note] {fs::remove_file(path).unwrap();}fs::remove_dir(directory).unwrap();
    }
}
