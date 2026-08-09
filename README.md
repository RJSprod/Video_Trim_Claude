# Video Trim

A touch-friendly video player built for pulling media *out* of video files: mark a
VLC-style A-B loop, watch it repeat, then export that exact span as a clip or grab
a full-resolution still — both straight to your Desktop.

The window is the video. Controls float on top as a frosted glass panel that blurs
the live frame behind it, and get out of the way when you're watching.

It comes two ways, sharing one engine:

| | |
| --- | --- |
| **WebUI** | A Gradio app in your browser on **port 7862**. One-click install into a local `venv`. |
| **Desktop app** | The original PySide6 window. `python app.py` |

Both cut clips with the same ffmpeg command and write to the same Desktop.

---

# The WebUI

## One-click install

Double-click the launcher for your platform:

| Platform | Launcher |
| --- | --- |
| Windows | `start_windows.bat` |
| Linux | `./start_linux.sh` |
| macOS | `./start_macos.sh` |

The first run creates a **`venv` folder inside this directory** and installs every
dependency into it, then opens <http://127.0.0.1:7862> in your browser. Later runs
see the venv is already good and start straight away.

Nothing is installed system-wide and nothing is written outside this folder, so
deleting the folder uninstalls everything. All you need beforehand is Python 3.9+
on `PATH`; the launcher checks and tells you where to get it if not.

```
Video_Trim_Claude/
  venv/            every dependency lives here          (created for you)
  cache/           uploads and transcoded previews      (created for you)
  CMD_FLAGS.txt    flags applied to every launch
```

To reinstall dependencies after pulling new code, run `update_wizard_windows.bat`
(or `./update_wizard_linux.sh`, `./update_wizard_macos.sh`). Add `--recreate` to
throw the venv away and build it from scratch.

## Port 7862

The WebUI is served on **127.0.0.1:7862**, and that port is treated as reserved:
if something else already holds it, the launcher stops and tells you what to look
for rather than quietly moving to 7863 and leaving you on a dead URL. Pass
`--any-port` if you would rather it take the next free one.

## Flags

Put them after the launcher (`./start_linux.sh --listen`) or one per line in
`CMD_FLAGS.txt` to apply them every time.

| Flag | Effect |
| --- | --- |
| `--listen` | Also serve on `0.0.0.0`, so other machines on your network can open it. |
| `--listen-port 7862` | Serve on a different port. |
| `--listen-host 192.168.1.5` | Bind one specific interface. |
| `--share` | Also expose a temporary public `gradio.live` URL. |
| `--no-browser` | Don't open a browser window on start. |
| `--any-port` | Use the next free port instead of stopping when 7862 is busy. |
| `--allow-remote-files` | Let visitors from other machines browse paths on **this** machine. |
| `--proxy-height 720` | Height of the preview built for codecs the browser can't play. |
| `--update` / `--recreate` / `--desktop` | Installer actions — see below. |

## Opening a video

Three ways, all landing in the same player:

- **Paste a path** into the box at the top and press Enter — a path on the machine
  running the server. Nothing is copied, so this is instant even for a 4K file.
- **Browse…** opens a file picker that walks that machine's folders, the WebUI's
  stand-in for the desktop app's Open dialog.
- **Drag a file onto the player**, or click *upload a file*. This one uploads a
  copy into `cache/uploads`, so prefer a path for anything large.

Browsing and opening by path are restricted to whoever is sitting at the machine
running the server. A visitor from elsewhere on the network gets a clear refusal
and has to upload instead — pass `--allow-remote-files` if you actually want
remote visitors reading that filesystem.

## Codecs the browser can't play

Playback is the browser's own video decoder, which covers less than the desktop
app's bundled FFmpeg: MP4/WebM with H.264, VP9 or AV1 play everywhere, while MKV,
AVI, WMV and HEVC often don't. When the browser refuses a file, the player says
so and offers **Build preview** — ffmpeg transcodes a scaled H.264 copy into
`cache/proxies` and plays that.

The preview is only ever a preview. **Clips and stills are always cut from the
original file**, at its full resolution, so nothing you save ever comes from the
transcode.

## Also installing the desktop app

The WebUI venv deliberately doesn't include PySide6 — it's a large download the
browser build never uses. To get both in one venv:

```
./start_linux.sh --install --desktop        # or start_windows.bat --install --desktop
```

Then `venv/bin/python app.py` (`venv\Scripts\python app.py` on Windows) runs the
Qt window from the same venv. The choice is remembered, so later launches keep it.

---

# The desktop app

## Install

```
pip install -r requirements.txt
```

That pulls in **PySide6** (playback, audio, UI) and **imageio-ffmpeg**, which supplies
the `ffmpeg` binary used to cut clips. If you already have ffmpeg on your `PATH`, or
you drop `ffmpeg.exe` next to `app.py`, that gets used instead.

## Run

```
python app.py
```

Optionally open a file straight away:

```
python app.py "C:\Users\you\Videos\clip.mp4"
```

The WebUI takes the same argument: `python webui.py "C:\...\clip.mp4"` opens the
browser straight into that file.

---

# Using it

Everything below is true of both front ends: the WebUI reproduces the desktop
app's control bar, gestures, keyboard map and A-B rules rather than inventing its
own.

## Controls

The bar along the bottom, left to right:

| Control | What it does |
| --- | --- |
| Scrubber | Drag or tap anywhere to seek, with live preview as you drag. Shows the A-B span and its markers. |
| Time | Current position (to a tenth) / total, plus the A-B range underneath. |
| ■ Stop | Pause and jump back to the start — marker A if a loop is set, otherwise 0:00. |
| « 5 | Back 5 seconds. |
| ▐◀ | Previous frame. Pauses first, then steps exactly one frame. |
| ▶ / ❚❚ | Play / pause. |
| ▶▌ | Next frame. |
| » 5 | Forward 5 seconds. |
| ⟳ Repeat | Loop the A-B range, or the whole file when no range is set. |
| **A-B** | Cycles the loop markers — see below. |
| ⭳ Save clip | Exports the current A-B range to the Desktop. Disabled until both markers exist. |
| ⛶ Screenshot | Saves the frame on screen to the Desktop as a PNG. |
| 🔊 Mute | Toggles audio. |
| 🗀 Open | Open a different video without leaving the session. |

You can also drag a video file onto the window at any time.

In the WebUI the same bar sits over the video as a real backdrop-blurred panel,
and 🗀 Open opens the host file picker described above.

### A-B looping

The A-B button cycles, exactly like VLC:

1. **First tap** — sets marker **A** at the current position.
2. **Second tap** — sets marker **B**. If you marked "backwards", the two are
   reordered rather than rejected.
3. **Third tap** — clears both.

Once both markers exist, **the range becomes the whole world** for playback:

- seeking and the ±5s skips clamp inside `[A, B]`;
- **Stop** returns to **A**, not to 0:00;
- pressing **Play** from outside the range snaps to **A** first;
- with **Repeat** on, playback wraps from B back to A continuously;
- with Repeat off, playback stops at B.

Clear the markers to get the full timeline back. Markers live for the session only —
they are never written to disk, and opening another video resets them.

### Scrubbing

The video follows the scrubber live while you drag, rather than jumping only when
you let go. Playback pauses for the duration of the drag so the preview keeps up
and the audio doesn't stutter, then **your play state is restored on release**:
drag while paused and it stays paused on the frame you landed on; drag during
playback and it resumes from there. Live seeks are rate limited to roughly one
every 60ms, and the exact release position is always applied.

### Frame stepping

The two buttons either side of play move exactly one frame, pausing first if
playback is running. Stepping is anchored on each decoded frame's own presentation
timestamp rather than on the player's reported position, so repeated steps don't
accumulate drift and stepping back lands on precisely the frame you left. The frame
rate is measured from the decoded frames themselves, falling back to the file's
declared rate. Steps clamp inside an A-B range like every other seek.

## Gestures

Designed for touch, and they work with a mouse too.

| Gesture | Action |
| --- | --- |
| Single tap | Show the controls and keep them up; tap again to dismiss. |
| Double tap, centre | Play / pause. |
| Double tap, left third | Back 5 seconds. |
| Double tap, right third | Forward 5 seconds. |

**A tap pins the controls.** Once you tap to bring them up they stay up — through
playback, pausing and mouse movement — until you tap the video again to dismiss
them. Nothing else takes them away.

The only controls that disappear on their own are ones you didn't ask for: on a
desktop, moving the mouse reveals the bar temporarily, and that reveal fades after
3 seconds of playback. Tapping while it is up promotes it to pinned rather than
hiding it, so a tap never dismisses a bar you didn't summon. Using the bar — a
button, the scrubber, even its background — resets that timer, and while **paused**
the controls never auto-hide at all.

## Keyboard

| Key | Action | | Key | Action |
| --- | --- | --- | --- | --- |
| `Space` | Play / pause | | `B` | Cycle A-B markers |
| `←` / `→` | ∓5 seconds | | `R` | Repeat |
| `,` / `.` | ∓1 frame | | `M` | Mute |
| `Shift+←` / `→` | ∓1 frame | | `S` | Screenshot |
| `↑` / `↓` | Volume | | `C` | Save the A-B clip |
| `Home` | Stop (back to A) | | `F` / `F11` | Fullscreen |
| `O` / `Ctrl+O` | Open a video | | `Esc` | Leave fullscreen |

---

## What lands on your Desktop

Both outputs are auto-named from the source file and the timecode, and never
overwrite — a repeat save becomes `… (2)`. OneDrive-redirected Desktops are
resolved through the Windows known-folder API, so files land where your Desktop
actually is; on Linux the localised `XDG_DESKTOP_DIR` is honoured.

**In the WebUI, "your Desktop" means the Desktop of the machine running the
server**, because ffmpeg runs there — the browser only drives the UI. When you're
running it locally that's the same thing. When you're reaching it from another
machine, the files land on the host and the player also offers each one as a
download link under the video. If the host has no Desktop at all (a headless box),
set `VIDEOTRIM_OUTPUT_DIR` to choose where saves go.

```
MyVideo_clip_01m23.4s_to_01m45.9s.mp4
MyVideo_frame_01m23.4s.png
```

**Clips** are cut frame-accurately at your markers — ffmpeg seeks to the preceding
keyframe, decodes forward and re-encodes (H.264 CRF 18 + AAC 192k, `+faststart`),
so the clip begins exactly where marker A is rather than snapping backwards to a
keyframe. A few seconds of work for a short clip. Export runs on a worker thread
with a live percentage, so the UI keeps playing throughout. Both front ends build
that ffmpeg command from the same function, so a clip cut in the browser is the
same job as one cut in the window.

**Screenshots** are the source frame at native resolution — a still from a 4K video
is 3840×2160 no matter how small the window is — with no controls baked in. The
desktop app saves the frame it already decoded; the WebUI asks ffmpeg for the frame
at that timestamp, which is the same pixels at the same size.

---

## Layout

```
app.py               desktop entry point
webui.py             WebUI entry point — argument parsing, port 7862, uvicorn
one_click.py         builds venv/ and launches; the launchers all call this
start_*.sh|bat       one-click launchers per platform
update_wizard_*      reinstall dependencies into the existing venv
CMD_FLAGS.txt        flags applied to every launch

videotrim/
  ── shared by both front ends, no Qt imports ──
  ffmpeg_tools.py    ffmpeg discovery, probing, clip/still/preview commands
  naming.py          output filenames
  paths.py           Desktop resolution, filename safety
  timefmt.py         time formatting

  ── desktop app ──
  main.py            window, wiring, shortcuts, drag & drop
  player.py          QMediaPlayer wrapper + all A-B loop semantics
  canvas.py          video painting, frosted panel, gestures, toasts
  controls.py        the floating control bar
  scrubber.py        seek bar with A-B rendering
  icons.py           vector glyphs and the glass buttons
  exporter.py        QThread wrapper around the shared ffmpeg commands
  theme.py           palette and metrics (Qt colours only)

  ── WebUI ──
  web/server.py      Gradio page + the /vt routes it talks to
  web/media.py       token registry and Range-aware streaming
  web/jobs.py        background ffmpeg jobs with pollable progress
  web/assets/        player.js and player.css — the browser player
```

In the desktop app, playback state lives in `player.py` and the UI observes it
through signals, so new controls generally mean adding a button in `controls.py`
and a method in `player.py`.

The WebUI is the same shape with the seam moved: `player.js` owns the A-B rules
(a direct port of `player.py`'s), Gradio provides the page and the source toolbar,
and the `/vt` routes do the work only the host can do — reading files, running
ffmpeg, writing to the Desktop. Because both front ends call into
`ffmpeg_tools.py`, changing how a clip is cut changes it in both.

## Notes

- **Desktop app:** frames are painted by the app rather than handed to a native
  video widget. That is what lets the control panel genuinely blur the video behind
  it and what makes full-resolution stills available instantly. The cost is CPU:
  1080p is comfortable, very high-bitrate 4K may drop frames on a slow machine.
  Codec support comes from Qt Multimedia's bundled FFmpeg backend, so the common
  formats (MP4/MKV/MOV/AVI/WebM, H.264/HEVC/VP9/AV1) play without a codec pack.
- **WebUI:** decoding is the browser's, which is narrower — hence the preview
  transcode for files it won't take. Video is streamed with byte-range requests so
  scrubbing a large file doesn't wait on a download, and files are addressed by
  opaque token rather than by path, so the only things reachable over HTTP are the
  ones you opened.
- Built and tested on Python 3.11, with PySide6 6.11 and Gradio 6. The WebUI keeps
  to long-stable Gradio API (`Blocks`, `HTML`, event `js=`, `mount_gradio_app`) and
  pins `gradio>=4.44,<7`.
