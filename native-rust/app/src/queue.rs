//! Queue edits preserve the playing entry and never start another audio stream.
use crate::Session;
use std::path::PathBuf;

impl Session {
    pub fn play_next(&mut self, path: PathBuf) -> Result<(), String> {
        if self.queue.len() >= 20_000 { return Err("Queue is full.".into()); }
        let at = self.cursor.map_or(0, |index| (index + 1).min(self.queue.len()));
        self.queue.insert(at, path);
        Ok(())
    }

    pub fn move_queued(&mut self, from: usize, to: usize) {
        if from >= self.queue.len() || to >= self.queue.len() || from == to { return; }
        let path = self.queue.remove(from);
        self.queue.insert(to, path);
        self.cursor = self.cursor.map(|current| {
            if current == from { to }
            else if from < current && to >= current { current - 1 }
            else if from > current && to <= current { current + 1 }
            else { current }
        });
    }

    pub fn remove_queued(&mut self, index: usize) -> Result<(), String> {
        if index >= self.queue.len() { return Err("Queue item no longer exists.".into()); }
        if self.cursor == Some(index) { return Err("Stop playback before removing the current track.".into()); }
        self.queue.remove(index);
        if let Some(current) = self.cursor.as_mut() { if index < *current { *current -= 1; } }
        Ok(())
    }

    /// Fisher-Yates for listening order, not for cryptographic purposes.
    pub fn shuffle_upcoming(&mut self, seed: u64) {
        let start = self.cursor.map_or(0, |index| index + 1).min(self.queue.len());
        let mut random = seed.max(1);
        for index in ((start + 1)..self.queue.len()).rev() {
            random ^= random << 13; random ^= random >> 7; random ^= random << 17;
            let swap = start + (random % (index - start + 1) as u64) as usize;
            self.queue.swap(index, swap);
        }
    }
}

#[cfg(test)] mod tests {
    use super::*;
    fn session() -> Session { Session { queue: ["a", "b", "c", "d", "e"].map(PathBuf::from).to_vec(), cursor: Some(1), position: 42.0, ..Default::default() } }
    #[test] fn edits_track_the_playing_entry_even_with_duplicates() {
        let mut value = session();
        value.play_next("b".into()).unwrap();
        assert_eq!(value.queue[2], PathBuf::from("b"));
        value.move_queued(1, 4);
        assert_eq!(value.cursor, Some(4));
        value.remove_queued(0).unwrap();
        assert_eq!(value.cursor, Some(3));
        assert!(value.remove_queued(3).is_err());
        assert_eq!(value.position, 42.0);
    }
    #[test] fn shuffle_keeps_played_prefix_and_every_upcoming_entry() {
        let mut value = session(); let before = value.queue.clone();
        value.shuffle_upcoming(1234);
        assert_eq!(value.queue[..2], before[..2]);
        let mut after = value.queue[2..].to_vec(); after.sort();
        assert_eq!(after, before[2..]); assert_eq!(value.cursor, Some(1));
        value.queue.clear(); value.cursor = None; value.shuffle_upcoming(0);
    }
}
