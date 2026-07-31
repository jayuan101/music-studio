# Music Studio

A Windows desktop app for getting music in, getting it right, and getting it
out in whatever format you need — at the best quality the source allows.

- **Play** anything in the library from a persistent transport bar — double-click
  a song to play it and queue everything else currently visible, with
  shuffle, repeat and a waveform playhead.
- **Search** YouTube directly from the Download page, and play a result the
  moment it downloads, to decide whether you actually want it.
- **Import** anything ffmpeg can decode (which is essentially everything).
- **Download** from YouTube or any of the 1700+ sites yt-dlp supports.
- **Convert** to FLAC, ALAC, WAV, AIFF, WavPack, MP3, AAC, Opus, Vorbis or WMA.
- **Edit** — trim, cut, boost volume well past the normal ceiling, normalise,
  fade, change speed and pitch, equalise, strip silence, remap channels —
  then either **Export** to a new file or **Save** the edits into the library
  file itself.
- **Tag** every field, across every container, with cover art fetched
  automatically from MusicBrainz, iTunes, Spotify and (as a last resort) a
  YouTube thumbnail. **Fix metadata** fills in whatever is missing from the
  filename and an online lookup; **YouTube Music format** goes further and
  reshapes tags to match YouTube Music's own conventions.
- **Ask** — a Personal AI assistant that can search, convert, edit, tag, or
  download for you in plain language. Runs on a local Ollama model with no
  network or API key by default; a Preferences toggle escalates specific
  commands to Claude when you want the extra capability.
- **Update itself** — checks GitHub for a newer release and installs it in
  place, from inside the app.

---

## The quality rule

Most converters quietly degrade audio. This one does not, and says so when it
cannot help:

| Rule | What it means |
|---|---|
| **One encode generation** | Edits and format changes compose into a *single* ffmpeg pass. Ten stacked effects still cost one encode, not ten. |
| **Nothing changes unasked** | Source sample rate and bit depth are preserved unless you change them or the target format genuinely cannot store them. |
| **No silent resampling** | When resampling is unavoidable, it uses `soxr` at precision 28, with triangular dither on any bit-depth reduction. |
| **Honest warnings** | Converting an MP3 to FLAC says plainly that it cannot restore detail and will only grow the file. It still lets you do it. |
| **Maximum encoder settings** | FLAC at compression level 8, MP3 at LAME V0, Opus at 192k VBR, AAC at 256k (using libfdk_aac where available). |

A `LOSSLESS` / `LOSSY` badge is visible wherever a file is shown, so you always
know what you are working with.

---

## Turning things up

The gain slider runs to **+30 dB (about 3000%)** by default — raise the ceiling
in Preferences if you need more — for quiet recordings, faint voice memos and
old rips. What happens past full scale is your choice:

- **Boost, then limit** *(default)* — a lookahead limiter catches peaks at a
  configurable true-peak ceiling (−0.3 dBTP by default). Genuinely loud, no
  clipping distortion.
- **Compress, then boost** — squashes peaks first so makeup gain lifts the whole
  track. The loudest option; costs dynamic range.
- **Raw gain** — no limiter, clipping allowed. **Measure clipping** tells you
  exactly how many samples flatten, so it is a decision rather than a surprise.

There is also a one-click **dynamic normalisation** for making everything loud,
and EBU R128 loudness normalisation (proper two-pass, not the inaccurate
single-pass form) for matching streaming levels.

All processing runs in 32-bit float, so intermediate stages have effectively
unlimited headroom and only the final encode quantises.

---

## Two players, on purpose

A persistent transport bar sits at the bottom of every page — double-click a
song anywhere in the Library and it plays, queuing every track currently
visible (respecting your search and sort), with previous/next, shuffle
(reorders only what's *ahead* of the current track, so turning it on
mid-album never replays what you just heard) and repeat (off, queue, or one
track).

The Editor has its own, separate, transport: play/pause (or the spacebar),
stop, a scrubber, a volume slider. Click anywhere on the waveform to seek
there; select a region and **Play selection** plays just that span.
**Preview with effects** is the one that matters — it renders a short excerpt
with your whole effect stack applied (gain, limiter, normalisation, EQ,
tempo, the lot) and plays that, so what you hear is what you would export.
Renders are debounced, so dragging a slider does not queue up a hundred of
them.

These are two independent playback engines rather than one shared player,
because previewing an edit and casually listening are different things you
might want to do at the same time in different tabs — starting one simply
pauses the other, so you never get two tracks fighting over the speakers.

When the edits are right, **Export** writes a new file, or **Save** applies
them straight to the file already in your library — encoded to a hidden
temp file first and swapped in atomically, so nothing at the real path
changes unless every step succeeds.

---

## Cover art and metadata

Four providers are tried in order, each one a fallback for when the last
found nothing:

1. **MusicBrainz → Cover Art Archive** — matches a specific release, so it is
   the most accurate. Rate-limited to 1 request/second as their rules require.
   No API key or signup.
2. **iTunes Search API** — broad and reliably high resolution: the 100×100
   thumbnail URL it returns is rewritten to fetch up to 1200×1200. No API key
   or signup.
3. **Spotify** — the best catalogue and cover-art match of the four, but the
   only one needing credentials: a free developer app's Client ID and Secret
   (Client Credentials flow — app-level access, no user login) from
   [developer.spotify.com/dashboard](https://developer.spotify.com/dashboard),
   entered in Preferences.
4. **YouTube thumbnail** — last resort, and scored accordingly: no
   credentials needed, but it is a video frame, not real album art, so
   quality and accuracy vary.

When more than one provider answers, a picker shows each candidate with its
source, resolution and match score rather than silently taking the first.

**Update all artwork** scans your library, fills in what is missing, and
optionally replaces embedded images below a resolution you set — which is what
"keep artwork up to date" means for a library full of decade-old 200px
thumbnails. Hits *and* misses are cached, so repeat scans are fast and stay
polite to free services. You can always drag in your own image instead.

**Fix metadata** does the same fallback search for text fields — title,
artist, album, year, genre — filling in only what is blank; an existing
value, however wrong, is never touched. Filenames are tried first (`Artist -
Title.ext`), then the same online chain above.

**YouTube Music format** is a deliberate step further, for a library built
from downloads: it *overwrites* fields, not just fills them, to match what
YouTube Music expects — album artist filled in on every track (the field it
groups an uploaded library by), guests moved out of the artist field and
into the title as `(feat. X)`, `(Official Video)`-style noise stripped from
titles, and duplicate genre spellings folded together. Current tags are
snapshotted first, so a run can be undone.

---

## Finding something new

The Download page has a search box alongside the usual paste-a-link field —
it searches YouTube directly (yt-dlp's own search, no separate API or
account). Double-click a result, or hit **Download & play**, and it downloads
through the same pipeline as any other link and starts playing the moment
it's done, so you can hear it before deciding whether to keep it. If not,
delete it from the Library like anything else.

---

## Downloading

Paste a link and pick one of two modes:

- **Keep the original stream** — no re-encoding, so no second generation of
  loss. This is the genuine best quality available.
- **Convert to a format you choose** — same pipeline as any other conversion.

Streaming sites serve lossy audio, so saving a download as FLAC makes a much
larger file without recovering anything. The app tells you that at the moment
you pick the format, rather than after the file is on disk.

Playlists are supported, with an optional limit. Downloads flow straight into
the tagging and artwork pipeline.

---

## Preferences

Everything has a default you can change, and changes save immediately:
output folder, filename template, whether to preserve source rate and depth,
which artwork providers to use (including Spotify credentials, stored in the
OS credential store when one is available) and minimum resolution, the
limiter ceiling, how far the gain slider reaches, the default download mode,
and whether new downloads are formatted for YouTube Music automatically.

Turning *off* "keep the source sample rate / bit depth" normalises everything
down to CD quality (44.1 kHz / 16-bit) — useful for shrinking a hi-res library
for a phone or car. Nothing is ever upsampled.

### Organising files

`filename_template` builds output names from tags, and a `/` creates
subfolders — the default is `{albumartist}/{album}/{track} - {title}`.
Track and disc numbers are zero-padded automatically. Available fields are
listed in Preferences. This only ever applies to files the app *writes*; it
never reorganises music already on your disk.

---

## Running it

### From a release

Download `MusicStudio-Windows-*.zip`, unzip it anywhere, run `MusicStudio.exe`.
ffmpeg is bundled — there is nothing else to install.

### Staying up to date

Preferences → Updates shows the installed version, with a **Check for
updates** button. It reads this repository's public GitHub Releases — no
account needed — and if a newer one exists, **Update now** downloads it and
installs it in place: a detached helper waits for the app to close, mirrors
the new build over the old one, and relaunches it. Only meaningful for a
release build; running from source, checking still works but there is
nothing to install in place.

### From source

```bash
pip install -r requirements.txt
python -m musicstudio
```

You will need `ffmpeg` and `ffprobe` on your PATH, or placed in
`vendor/ffmpeg/`.

### Building the Windows executable

```bash
pip install -r requirements-dev.txt
# put ffmpeg.exe and ffprobe.exe in vendor/ffmpeg/
pyinstaller --noconfirm --clean MusicStudio.spec
```

Output lands in `dist/MusicStudio/`. CI does this automatically — see
`.github/workflows/build.yml`, which runs on `v*` tags or on demand, and
downloads ffmpeg itself.

---

## Tests

```bash
python -m pytest tests -q
```

382 tests covering the filter-graph builder, the quality policy, real encodes
into all ten output formats, tag round-trips through every container, the
four-provider artwork/metadata fallback chain (against mocked HTTP), the
YouTube Music tag normaliser (including the real edge cases it was built to
fix — a feature credit swallowing the song name, a single mistaken for a
video title, a mixtape mistaken for a compilation), the playback queue
(shuffle, repeat, running off either end), filename templating and path
safety, settings persistence, and the library index. Tests that need audio
generate it with ffmpeg rather than committing binary fixtures.

---

## How it fits together

```
musicstudio/
├── core/
│   ├── ffmpeg.py     binary resolution, progress parsing, cancellation
│   ├── probe.py      AudioInfo, lossless detection, clipping measurement
│   ├── formats.py    the ten output formats and their best settings
│   ├── convert.py    quality policy + single-pass command builder
│   ├── edit.py       the filter-graph builder
│   ├── tags.py       one TagSet over ID3 / Vorbis / MP4 / APEv2 / ASF
│   ├── artwork.py    MusicBrainz, Cover Art Archive, iTunes, Spotify, YouTube
│   ├── spotify.py    Client Credentials auth + track search
│   ├── tag_fix.py    fills blank tags from the filename, then online
│   ├── ytmusic.py    reshapes tags to YouTube Music's conventions
│   ├── download.py   yt-dlp wrapper, plus search()
│   ├── organise.py   filename templating, with Windows path safety
│   ├── updater.py    checks GitHub releases, installs in place
│   ├── secrets.py    OS credential store for the Claude key and Spotify secret
│   └── jobs.py       background queue with progress and cancel
├── db.py             SQLite library index
└── ui/
    ├── widgets/now_playing.py   the persistent player and its queue
    └── ...                      panels, waveform, editor transport, activity dock
```

`core/` has no dependency on Qt, so the whole engine is testable headlessly.

---

## Notes

- **Licence:** mutagen is GPL-2.0-or-later and the bundled ffmpeg is a GPL
  build, so the distributed application is effectively GPL.
- **Download size.** The Windows build bundles both a full ffmpeg and Qt's
  multimedia stack, so it is a few hundred megabytes. That is the cost of an
  app that works the moment you unzip it, with nothing else to install.
- **Downloading from YouTube** conflicts with its Terms of Service. Sensible for
  your own content or content you hold rights to.
- **No tool can make a lossy file lossless.** Where that matters, the app says
  so at the point of the decision instead of implying an upgrade.
