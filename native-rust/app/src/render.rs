use std::{ffi::OsString, path::Path};

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ExportFormat { Flac, Alac, Wav, Aiff, WavPack, Mp3, Aac, Opus, Vorbis, Wma }
impl ExportFormat {
    pub const ALL: [Self; 10] = [Self::Flac,Self::Alac,Self::Wav,Self::Aiff,Self::WavPack,Self::Mp3,Self::Aac,Self::Opus,Self::Vorbis,Self::Wma];
    pub fn extension(self) -> &'static str { match self {
        Self::Flac=>"flac",Self::Alac|Self::Aac=>"m4a",Self::Wav=>"wav",Self::Aiff=>"aiff",Self::WavPack=>"wv",Self::Mp3=>"mp3",Self::Opus=>"opus",Self::Vorbis=>"ogg",Self::Wma=>"wma"
    } }
    pub fn label(self) -> &'static str { match self {
        Self::Flac=>"FLAC",Self::Alac=>"Apple Lossless",Self::Wav=>"WAV",Self::Aiff=>"AIFF",Self::WavPack=>"WavPack",Self::Mp3=>"MP3 V0",Self::Aac=>"AAC 256k",Self::Opus=>"Opus 192k",Self::Vorbis=>"Vorbis Q8",Self::Wma=>"WMA 320k"
    } }
    pub fn lossless(self) -> bool { matches!(self,Self::Flac|Self::Alac|Self::Wav|Self::Aiff|Self::WavPack) }
}

#[derive(Clone, Debug)]
pub struct SourceAudio { pub rate:u32, pub channels:u32, pub bits:Option<u32>, pub float:bool, pub duration:Option<f64> }

#[derive(Clone, Debug)]
pub struct Edit {
    pub start:f64, pub end:Option<f64>, pub gain_db:f64, pub tempo:f64,
    pub fade_in:f64, pub fade_out:f64, pub bass_db:f64, pub treble_db:f64,
    pub limiter:bool, pub dynamic_normalize:bool,
    /// Zero preserves source rate, precision and channel count respectively.
    pub rate:u32, pub bits:u32, pub channels:u32,
}
impl Default for Edit {
    fn default()->Self { Self {start:0.0,end:None,gain_db:0.0,tempo:1.0,fade_in:0.0,fade_out:0.0,bass_db:0.0,treble_db:0.0,limiter:true,dynamic_normalize:false,rate:0,bits:0,channels:0} }
}

pub fn render_args(source:&Path,destination:&Path,format:ExportFormat,audio:&SourceAudio,edit:&Edit)->Result<Vec<OsString>,String> {
    if super::key(source)==super::key(destination) {return Err("Export must use a new file, not the source.".into());}
    for (value,min,max,label) in [(edit.start,0.0,31_536_000.0,"Trim start"),(edit.gain_db,-60.0,30.0,"Gain"),(edit.tempo,0.5,2.0,"Speed"),(edit.fade_in,0.0,3600.0,"Fade in"),(edit.fade_out,0.0,3600.0,"Fade out"),(edit.bass_db,-20.0,20.0,"Bass"),(edit.treble_db,-20.0,20.0,"Treble")] {
        if !value.is_finite() || !(min..=max).contains(&value) {return Err(format!("{label} must be between {min} and {max}."));}
    }
    if edit.end.is_some_and(|end|!end.is_finite()||end<=edit.start) {return Err("Trim end must be after the start.".into());}
    let source_duration=audio.duration.filter(|d|d.is_finite()&&*d>0.0);
    if source_duration.is_some_and(|d|edit.start>=d || edit.end.is_some_and(|end|end>d+0.05)) {return Err("Trim range extends beyond the source audio.".into());}
    let seconds=edit.end.or(source_duration).map(|end|(end-edit.start)/edit.tempo);
    if edit.fade_in>0.0 || edit.fade_out>0.0 {
        let seconds=seconds.ok_or("Cannot determine duration for fades. Specify a trim end.")?;
        if edit.fade_in+edit.fade_out>seconds {return Err("Fades are longer than the edited audio. Shorten the fades.".into());}
    }
    let rate=if edit.rate==0 {audio.rate}else{edit.rate};
    if !(8000..=384000).contains(&rate) {return Err("Choose a sample rate between 8000 and 384000 Hz.".into());}
    let rates:&[u32]=match format {
        ExportFormat::Mp3=>&[8000,11025,12000,16000,22050,24000,32000,44100,48000],
        ExportFormat::Aac=>&[8000,11025,12000,16000,22050,24000,32000,44100,48000,64000,88200,96000],
        ExportFormat::Opus=>&[8000,12000,16000,24000,48000],
        ExportFormat::Wma=>&[8000,11025,16000,22050,32000,44100,48000], _=>&[]
    };
    if !rates.is_empty()&&!rates.contains(&rate) {return Err(format!("{} cannot encode {rate} Hz. Choose an explicit supported output rate; no automatic resampling was performed.",format.label()));}
    let channels=if edit.channels==0 {audio.channels}else{edit.channels};
    let max_channels=match format {ExportFormat::Mp3|ExportFormat::Wma=>2,ExportFormat::Wav|ExportFormat::Aiff=>32,_=>8};
    if channels==0||channels>max_channels {return Err(format!("{} supports up to {max_channels} channels. Select mono/stereo explicitly if a downmix is intended.",format.label()));}
    if ![0,16,24,32].contains(&edit.bits) {return Err("Choose source precision, 16, 24 or 32 bits.".into());}
    let float=audio.float&&edit.bits==0;
    let bits=if edit.bits==0 {match audio.bits.unwrap_or(24) {0..=16=>16,17..=24=>24,25..=32=>32,other=>other}}else{edit.bits};
    if format.lossless() && (bits>32 || (float&&!matches!(format,ExportFormat::Wav|ExportFormat::Aiff|ExportFormat::WavPack)) || (bits>24&&matches!(format,ExportFormat::Flac|ExportFormat::Alac))) {
        return Err("This output cannot preserve the source precision. Choose WAV/WavPack, or explicitly select a lower integer bit depth.".into());
    }
    let mut args:Vec<OsString>=["-nostdin","-hide_banner","-loglevel","error","-n","-protocol_whitelist","file","-i"].into_iter().map(Into::into).collect();
    args.push(source.as_os_str().into());
    args.extend(["-map","0:a:0","-vn","-map_metadata","0"].into_iter().map(Into::into));
    let mut filters=vec![];
    if edit.start>0.0||edit.end.is_some() {
        filters.push(match edit.end {Some(end)=>format!("atrim=start={:.6}:end={end:.6}",edit.start),None=>format!("atrim=start={:.6}",edit.start)});
        filters.push("asetpts=PTS-STARTPTS".into());
    }
    let effects=edit.gain_db!=0.0||edit.tempo!=1.0||edit.fade_in>0.0||edit.fade_out>0.0||edit.bass_db!=0.0||edit.treble_db!=0.0||edit.dynamic_normalize;
    if effects {filters.push("aformat=sample_fmts=fltp".into());}
    if edit.tempo!=1.0 {filters.push(format!("atempo={:.6}",edit.tempo));}
    if edit.bass_db!=0.0 {filters.push(format!("bass=g={:.6}",edit.bass_db));}
    if edit.treble_db!=0.0 {filters.push(format!("treble=g={:.6}",edit.treble_db));}
    if edit.dynamic_normalize {filters.push("dynaudnorm".into());}
    if edit.gain_db!=0.0 {filters.push(format!("volume={:.6}dB",edit.gain_db));}
    if edit.limiter&&effects {filters.push("alimiter=limit=0.96605088:level=false:latency=true".into());}
    if edit.fade_in>0.0 {filters.push(format!("afade=t=in:st=0:d={:.6}",edit.fade_in));}
    if edit.fade_out>0.0 {filters.push(format!("afade=t=out:st={:.6}:d={:.6}",seconds.unwrap()-edit.fade_out,edit.fade_out));}
    let reduced=audio.float&&!float || audio.bits.is_some_and(|old|bits<old);
    if rate!=audio.rate || (format.lossless()&&reduced) {
        let sample=if format.lossless()&&!float {if bits<=16 {":osf=s16"}else{":osf=s32"}}else{""};
        let dither=if format.lossless()&&reduced {format!(":dither_method=triangular:output_sample_bits={bits}")}else{String::new()};
        filters.push(format!("aresample={rate}:resampler=soxr:precision=28{sample}{dither}"));
    }
    if !filters.is_empty() {args.extend(["-af".into(),filters.join(",").into()]);}
    args.extend(["-ar".into(),rate.to_string().into(),"-ac".into(),channels.to_string().into(),"-c:a".into()]);
    let pcm=|endian:&str|if float {format!("pcm_f32{endian}")}else{format!("pcm_s{bits}{endian}")};
    match format {
        ExportFormat::Flac=>args.extend(["flac","-compression_level","8"].into_iter().map(Into::into)),
        ExportFormat::Alac=>args.extend(["alac","-f","ipod"].into_iter().map(Into::into)),
        ExportFormat::Wav=>args.push(pcm("le").into()),
        ExportFormat::Aiff=>args.push(pcm("be").into()),
        ExportFormat::WavPack=>args.extend(["wavpack","-compression_level","3"].into_iter().map(Into::into)),
        ExportFormat::Mp3=>args.extend(["libmp3lame","-q:a","0"].into_iter().map(Into::into)),
        ExportFormat::Aac=>args.extend(["aac","-b:a","256k","-f","ipod"].into_iter().map(Into::into)),
        ExportFormat::Opus=>args.extend(["libopus","-b:a","192k","-vbr","on","-compression_level","10","-application","audio"].into_iter().map(Into::into)),
        ExportFormat::Vorbis=>args.extend(["libvorbis","-q:a","8"].into_iter().map(Into::into)),
        ExportFormat::Wma=>args.extend(["wmav2","-b:a","320k"].into_iter().map(Into::into)),
    }
    if format.lossless()&&!matches!(format,ExportFormat::Wav|ExportFormat::Aiff) {
        let sample=match format {ExportFormat::WavPack=>if float {"fltp"}else if bits<=16 {"s16p"}else{"s32p"},ExportFormat::Alac=>if bits<=16 {"s16p"}else{"s32p"},_=>if bits<=16 {"s16"}else{"s32"}};
        args.extend(["-sample_fmt".into(),sample.into()]);
        if !float {args.extend(["-bits_per_raw_sample".into(),bits.to_string().into()]);}
    }
    args.push(destination.as_os_str().into());Ok(args)
}

#[cfg(test)] mod tests {
    use super::*;
    fn source()->SourceAudio {SourceAudio {rate:48000,channels:2,bits:Some(24),float:false,duration:Some(1.0)}}
    fn plan(format:ExportFormat,audio:&SourceAudio,edit:&Edit)->Result<Vec<OsString>,String> {render_args(Path::new("SOURCE.wav"),Path::new(&format!("OUTPUT.{}",format.extension())),format,audio,edit)}
    #[test] fn rejects_implicit_quality_loss_and_invalid_edits() {
        let mut audio=source();audio.rate=44100;
        assert!(plan(ExportFormat::Opus,&audio,&Edit::default()).is_err());
        audio.rate=48000;audio.bits=Some(32);
        assert!(plan(ExportFormat::Alac,&audio,&Edit::default()).is_err());
        let e=Edit {bits:24,..Default::default()};
        assert!(plan(ExportFormat::Alac,&audio,&e).unwrap().iter().any(|a|a.to_string_lossy().contains("dither_method=triangular")));
        assert!(plan(ExportFormat::Wav,&audio,&Edit {tempo:f64::NAN,..Default::default()}).is_err());
        assert!(plan(ExportFormat::Wav,&audio,&Edit {fade_out:2.0,..Default::default()}).is_err());
        assert!(plan(ExportFormat::Wav,&audio,&Edit {start:2.0,..Default::default()}).is_err());
    }
    #[test] fn emits_real_render_plans_for_ffmpeg_acceptance() {
        for format in ExportFormat::ALL {
            let args=plan(format,&source(),&Edit::default()).unwrap();
            assert!(args.contains(&"-n".into()));
            let args:Vec<_>=args.iter().map(|a|a.to_string_lossy().into_owned()).collect();
            println!("RENDER_PLAN {}",serde_json::json!({"name":format.label(),"extension":format.extension(),"args":args,"rate":48000,"channels":2,"duration":1.0}));
        }
        let edit=Edit {start:0.1,end:Some(0.9),tempo:2.0,gain_db:12.0,bass_db:3.0,treble_db:-2.0,fade_in:0.05,fade_out:0.05,rate:44100,bits:16,channels:1,..Default::default()};
        let args:Vec<_>=plan(ExportFormat::Wav,&source(),&edit).unwrap().iter().map(|a|a.to_string_lossy().into_owned()).collect();
        println!("RENDER_PLAN {}",serde_json::json!({"name":"edited","extension":"wav","args":args,"rate":44100,"channels":1,"duration":0.4}));
    }
}
