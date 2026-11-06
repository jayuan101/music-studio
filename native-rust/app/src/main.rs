#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]
mod media;
use eframe::egui;
use music_studio_native::{next_index, playlist_text, ExportFormat, Playlist, Repeat, Session, Track};
use rodio::{Decoder, OutputStream, OutputStreamBuilder, Sink, Source};
use std::{fs::File,path::PathBuf,sync::mpsc::{self,Receiver},time::{Duration,SystemTime,UNIX_EPOCH}};

enum Work { Imported(Vec<Track>,usize), Exported(PathBuf) }
struct Studio {
    root:PathBuf, state:Session, stream:Option<OutputStream>, sink:Option<Sink>, duration:Option<f64>,
    selected:Option<PathBuf>, playlist:Option<usize>, search:String, playlist_name:String,
    status:String,error:Option<String>,pending:Option<Receiver<Result<Work,String>>>,
    export_format:ExportFormat,trim_start:f64,trim_end:f64,trim_enabled:bool,
}
impl Studio {
    fn new()->Self {
        let root=PathBuf::from(r"D:\UserData\Apps\MusicStudio-Rust");
        let loaded=transcript_store::latest_snapshot::<Session>(&root.join("sessions"));
        let mut error=loaded.as_ref().err().cloned();
        let mut state=loaded.ok().flatten().unwrap_or_default();
        if let Err(message)=state.validate() {error=Some(message);state=Session::default();}
        Self {root,state,stream:None,sink:None,duration:None,selected:None,playlist:None,search:String::new(),playlist_name:String::new(),
            status:"Add files or a folder. Saved queues reopen paused.".into(),error,pending:None,
            export_format:ExportFormat::Flac,trim_start:0.0,trim_end:30.0,trim_enabled:false}
    }
    fn save(&mut self) -> bool {
        if let Some(sink)=&self.sink {self.state.position=sink.get_pos().as_secs_f64();}
        match transcript_store::save_snapshot(&self.root.join("sessions"),&self.state) {
            Ok(_)=>{self.status="Library, playlists, queue, and playback position saved on D:.".into();true},Err(error)=>{self.error=Some(error);false}
        }
    }
    fn start_job(&mut self, job:impl FnOnce()->Result<Work,String>+Send+'static) {
        if self.pending.is_some() {return;}let (tx,rx)=mpsc::channel();self.pending=Some(rx);self.error=None;
        std::thread::spawn(move||{let _=tx.send(job());});
    }
    fn import(&mut self,paths:Vec<PathBuf>) {self.status="Reading selected audio files...".into();self.start_job(move||media::scan(paths).map(|(tracks,skipped)|Work::Imported(tracks,skipped)));}
    fn poll(&mut self) {
        if let Some(result)=self.pending.as_ref().map(|rx|rx.try_recv()) {
            match result {
                Ok(Ok(Work::Imported(tracks,skipped)))=>{self.pending=None;let added=self.state.add_tracks(tracks);self.save();self.status=format!("Added {added} tracks; skipped {skipped} unsupported, unreadable, or linked items.");},
                Ok(Ok(Work::Exported(path)))=>{self.pending=None;self.status=format!("Export saved: {}",path.display());},
                Ok(Err(error))=>{self.pending=None;self.error=Some(error);self.status="Operation failed. Source files were not changed.".into();},
                Err(mpsc::TryRecvError::Disconnected)=>{self.pending=None;self.error=Some("Background operation stopped unexpectedly.".into());},
                Err(mpsc::TryRecvError::Empty)=>{}
            }
        }
        if self.sink.as_ref().is_some_and(|sink|sink.empty()&&!sink.is_paused()) {self.advance(false);}
    }
    fn play_at(&mut self,index:usize,position:f64)->Result<(),String> {
        let path=self.state.queue.get(index).ok_or("Queue is empty.")?;
        let decoder=Decoder::try_from(File::open(path).map_err(|_|format!("Cannot open {}. The missing entry is retained.",path.display()))?).map_err(|error|format!("Cannot decode this file: {error}"))?;
        let duration=decoder.total_duration().map(|time|time.as_secs_f64());
        if self.stream.is_none() {self.stream=Some(OutputStreamBuilder::open_default_stream().map_err(|error|format!("Cannot open the audio output: {error}"))?);}
        let sink=Sink::connect_new(self.stream.as_ref().unwrap().mixer());sink.pause();sink.set_volume(self.state.volume);sink.append(decoder);
        if position>0.0 {sink.try_seek(Duration::from_secs_f64(position)).map_err(|error|format!("Cannot restore position: {error}. Start the track from the library."))?;}
        if let Some(old)=self.sink.take() {old.stop();} sink.play();self.sink=Some(sink);self.duration=duration;
        self.state.cursor=Some(index);self.state.position=position;self.error=None;self.status=format!("Playing {}",path.file_name().unwrap_or_default().to_string_lossy());Ok(())
    }
    fn advance(&mut self,manual:bool) {
        let next=self.state.cursor.and_then(|index|next_index(index,self.state.queue.len(),self.state.repeat,manual));
        if let Some(index)=next {if let Err(error)=self.play_at(index,0.0) {self.error=Some(error);if let Some(sink)=self.sink.take(){sink.stop();}}}
        else {if let Some(sink)=self.sink.take(){sink.stop();}self.state.cursor=None;self.state.position=0.0;self.status="Queue finished.".into();}
    }
    fn play_paths(&mut self,paths:Vec<PathBuf>,index:usize) {
        self.state.queue=paths;
        if let Err(error)=self.play_at(index,0.0){
            if let Some(sink)=self.sink.take(){sink.stop();}
            self.state.cursor=None;self.state.position=0.0;self.error=Some(error);
        }
    }
    fn visible(&self)->Vec<Track> {
        let query=self.search.to_lowercase();
        let rows:Vec<_>=match self.playlist.and_then(|index|self.state.playlists.get(index)) {
            Some(playlist)=>playlist.paths.iter().map(|path|self.state.library.iter().find(|track|&track.path==path).cloned().unwrap_or_else(||Track {path:path.clone(),title:path.file_stem().unwrap_or_default().to_string_lossy().into_owned(),seconds:None})).collect(),
            None=>self.state.library.clone(),
        };
        rows.into_iter().filter(|track|track.title.to_lowercase().contains(&query)).collect()
    }
    fn export_selected(&mut self) {
        let Some(source)=self.selected.clone() else{return;};
        let ffmpeg=self.state.ffmpeg.clone();let root=self.root.clone();let format=self.export_format;
        let start=if self.trim_enabled {self.trim_start}else{0.0};let end=self.trim_enabled.then_some(self.trim_end);
        self.status="Exporting a new copy...".into();self.start_job(move||media::export(&ffmpeg,&source,&root,format,start,end).map(Work::Exported));
    }
    fn playlist_export(&mut self) {
        let paths=self.playlist.and_then(|index|self.state.playlists.get(index)).map(|list|&list.paths).unwrap_or(&self.state.queue);
        let result=(||{
            let content=playlist_text(paths)?;let folder=self.root.join("playlists");std::fs::create_dir_all(&folder).map_err(|error|error.to_string())?;
            let stamp=SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default().as_nanos();let path=folder.join(format!("playlist-{stamp}.m3u8"));
            transcript_store::write_new(&path,content.as_bytes())?;Ok::<_,String>(path)
        })();match result {Ok(path)=>self.status=format!("Playlist exported: {}",path.display()),Err(error)=>self.error=Some(error)}
    }
    fn sidebar(&mut self,ui:&mut egui::Ui) {
        ui.heading("Your music");
        ui.add_enabled_ui(self.pending.is_none(),|ui| {
            if ui.button("Add audio files...").clicked() {if let Some(paths)=rfd::FileDialog::new().add_filter("Audio",&["mp3","flac","wav","ogg","m4a","aac","aiff","mp4"]).pick_files(){self.import(paths);}}
            if ui.button("Add folder...").clicked() {if let Some(path)=rfd::FileDialog::new().pick_folder(){self.import(vec![path]);}}
        });
        ui.small("Imports references to your files. Originals stay in place.");
        ui.separator();if ui.selectable_label(self.playlist.is_none(),format!("Library ({})",self.state.library.len())).clicked(){self.playlist=None;}
        for (index,list) in self.state.playlists.iter().enumerate() {if ui.selectable_label(self.playlist==Some(index),&list.name).clicked(){self.playlist=Some(index);}}
        ui.separator();ui.label("New playlist name");ui.text_edit_singleline(&mut self.playlist_name);
        if ui.add_enabled(!self.state.queue.is_empty(),egui::Button::new("Save queue as playlist")).clicked() {
            let name=self.playlist_name.trim().to_owned();
            if name.is_empty()||name.len()>100||self.state.playlists.iter().any(|list|list.name.eq_ignore_ascii_case(&name))||self.state.playlists.len()>=200 {self.error=Some("Use a unique playlist name (1–100 bytes); up to 200 playlists.".into());}
            else {self.state.playlists.push(Playlist {name,paths:self.state.queue.clone()});self.playlist_name.clear();self.save();}
        }
        if ui.button("Export playlist / queue as M3U8").clicked(){self.playlist_export();}
        ui.separator();if ui.button("Save session").clicked(){self.save();}
        if ui.button("Open D: data folder").clicked(){match std::fs::create_dir_all(&self.root).and_then(|_|std::process::Command::new("explorer.exe").arg(&self.root).spawn().map(|_|())){Ok(())=>{},Err(error)=>self.error=Some(error.to_string())}}
        ui.separator();ui.heading("Export selected track");
        ui.add_enabled_ui(self.pending.is_none()&&self.selected.is_some(),|ui|{
            ui.horizontal(|ui|{ui.selectable_value(&mut self.export_format,ExportFormat::Flac,"FLAC");ui.selectable_value(&mut self.export_format,ExportFormat::Mp3,"MP3 V0");});
            ui.small("MP3 is lossy. FLAC cannot restore detail missing from a lossy source.");
            ui.checkbox(&mut self.trim_enabled,"Trim exported copy");if self.trim_enabled {ui.horizontal(|ui|{ui.label("Start s");ui.add(egui::DragValue::new(&mut self.trim_start).range(0.0..=86_400.0));});ui.horizontal(|ui|{ui.label("End s");ui.add(egui::DragValue::new(&mut self.trim_end).range(0.0..=86_400.0));});}
            if ui.button("Choose ffmpeg.exe...").clicked(){if let Some(path)=rfd::FileDialog::new().add_filter("FFmpeg executable",&["exe"]).pick_file(){self.state.ffmpeg=path;}}
            if ui.button("Export new file on D:").clicked(){self.export_selected();}
        });
        ui.small("Advanced editing, tags, artwork, downloads, AI, and updates are not ported yet.");
    }
    fn transport(&mut self,ui:&mut egui::Ui) {
        ui.horizontal(|ui|{
            if ui.button("Previous").clicked(){if let Some(index)=self.state.cursor{if let Err(error)=self.play_at(index.saturating_sub(1),0.0){self.error=Some(error);}}}
            let paused=self.sink.as_ref().is_none_or(|sink|sink.is_paused());
            if ui.button(if paused{"Play"}else{"Pause"}).clicked(){
                if let Some(sink)=&self.sink {if paused{sink.play();}else{sink.pause();}}
                else if !self.state.queue.is_empty(){if let Err(error)=self.play_at(self.state.cursor.unwrap_or(0),self.state.position){self.error=Some(error);}}
            }
            if ui.button("Stop").clicked(){if let Some(sink)=self.sink.take(){sink.stop();}self.state.position=0.0;self.state.cursor=None;}
            if ui.button("Next").clicked(){self.advance(true);}
            ui.label("Repeat");ui.selectable_value(&mut self.state.repeat,Repeat::Off,"Off");ui.selectable_value(&mut self.state.repeat,Repeat::All,"Queue");ui.selectable_value(&mut self.state.repeat,Repeat::One,"Track");
            if ui.add(egui::Slider::new(&mut self.state.volume,0.0..=1.0).text("Volume")).changed(){if let Some(sink)=&self.sink{sink.set_volume(self.state.volume);}}
        });
        if let Some(sink)=&self.sink {let mut seconds=sink.get_pos().as_secs_f64();self.state.position=seconds;
            if let Some(duration)=self.duration.filter(|value|*value>0.0){if ui.add(egui::Slider::new(&mut seconds,0.0..=duration).text("Position (seconds)")).changed(){if let Err(error)=sink.try_seek(Duration::from_secs_f64(seconds)){self.error=Some(error.to_string());}}}
            else {ui.label(format!("{seconds:.1} seconds"));}
        }
        if let Some(index)=self.state.cursor {if let Some(path)=self.state.queue.get(index){ui.label(path.file_name().unwrap_or_default().to_string_lossy());}}
    }
}
impl eframe::App for Studio {
    fn update(&mut self,ctx:&egui::Context,_:&mut eframe::Frame) {
        self.poll();
        if ctx.input(|input|input.viewport().close_requested()) {
            if self.pending.is_some(){ctx.send_viewport_cmd(egui::ViewportCommand::CancelClose);self.error=Some("Wait for the current import/export to finish before closing.".into());}
            else if !self.save(){ctx.send_viewport_cmd(egui::ViewportCommand::CancelClose);}
        }
        egui::TopBottomPanel::top("heading").show(ctx,|ui|{ui.heading("Music Studio Rust");ui.label("Native preview · library, playlists, playback, and export copies");});
        egui::TopBottomPanel::bottom("transport").show(ctx,|ui|{self.transport(ui);ui.separator();if self.pending.is_some(){ui.spinner();}ui.label(&self.status);if let Some(error)=&self.error{ui.colored_label(egui::Color32::LIGHT_RED,error);}if ui.button("Dismiss error").clicked(){self.error=None;}});
        egui::SidePanel::left("sidebar").default_width(275.0).show(ctx,|ui|{egui::ScrollArea::vertical().show(ui,|ui|self.sidebar(ui));});
        egui::SidePanel::right("queue").default_width(250.0).show(ctx,|ui|{
            ui.heading("Play queue");
            if ui.button("Shuffle upcoming tracks").clicked(){
                let seed=SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default().as_nanos() as u64;
                self.state.shuffle_upcoming(seed);
            }
            ui.small("Shuffle leaves the current track and played tracks in place.");
            let mut move_item=None;let mut remove=None;let mut play=None;
            egui::ScrollArea::vertical().id_salt("queue_scroll").show(ui,|ui|{
                for (index,path) in self.state.queue.iter().enumerate(){ui.push_id(index,|ui|{
                    let label=path.file_name().unwrap_or_default().to_string_lossy();
                    if ui.selectable_label(self.state.cursor==Some(index),label).double_clicked(){play=Some(index);}
                    ui.horizontal(|ui|{
                        if ui.add_enabled(index>0,egui::Button::new("Up")).clicked(){move_item=Some((index,index-1));}
                        if ui.add_enabled(index+1<self.state.queue.len(),egui::Button::new("Down")).clicked(){move_item=Some((index,index+1));}
                        if ui.add_enabled(self.state.cursor!=Some(index),egui::Button::new("Remove")).clicked(){remove=Some(index);}
                    });
                });}
            });
            if let Some((from,to))=move_item{self.state.move_queued(from,to);}
            if let Some(index)=remove{if let Err(error)=self.state.remove_queued(index){self.error=Some(error);}}
            if let Some(index)=play{if let Err(error)=self.play_at(index,0.0){self.error=Some(error);}}
        });
        egui::CentralPanel::default().show(ctx,|ui|{
            ui.horizontal(|ui|{ui.label("Search filenames");ui.text_edit_singleline(&mut self.search);});let rows=self.visible();
            ui.horizontal(|ui|{
                if ui.add_enabled(!rows.is_empty(),egui::Button::new("Play visible tracks")).clicked(){self.play_paths(rows.iter().map(|track|track.path.clone()).collect(),0);}
                if ui.add_enabled(self.selected.is_some(),egui::Button::new("Add selected to queue")).clicked(){if self.state.queue.len()<20_000{self.state.queue.push(self.selected.clone().unwrap());}}
                if ui.add_enabled(self.selected.is_some(),egui::Button::new("Play selected next")).clicked(){if let Some(path)=self.selected.clone(){if let Err(error)=self.state.play_next(path){self.error=Some(error);}}}
                ui.label(format!("{} tracks · {} queued",rows.len(),self.state.queue.len()));
            });ui.separator();
            egui::ScrollArea::vertical().show(ui,|ui|{for (index,track) in rows.iter().enumerate(){ui.horizontal(|ui|{
                let response=ui.selectable_label(self.selected.as_ref()==Some(&track.path),&track.title).on_hover_text(track.path.to_string_lossy());
                if response.clicked(){self.selected=Some(track.path.clone());}if response.double_clicked(){self.selected=Some(track.path.clone());self.play_paths(rows.iter().map(|track|track.path.clone()).collect(),index);}
                if let Some(seconds)=track.seconds{ui.weak(format!("{}:{:02}",seconds as u64/60,seconds as u64%60));}
            });}});
        });
        if self.pending.is_some()||self.sink.is_some(){ctx.request_repaint_after(Duration::from_millis(200));}
    }
}
fn main()->eframe::Result {
    eframe::run_native("Music Studio Rust",eframe::NativeOptions {viewport:egui::ViewportBuilder::default().with_inner_size([1180.0,820.0]).with_min_inner_size([850.0,600.0]),..Default::default()},Box::new(|_|Ok(Box::new(Studio::new()))))
}
