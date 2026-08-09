# Video Trim

A touch-friendly video player built for pulling media *out* of video files: mark a
VLC-style A-B loop, watch it repeat, then export that exact span as a clip or grab
a full-resolution still — both straight to your Desktop.

The window is the video. Controls float on top as a frosted glass panel that blurs
the live frame behind it, and get out of the way when you're watching.

---

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

---

## Controls

The bar along the bottom, left to right:

| Control | What it does |
| --- | --- |
| Scrubber | Drag or tap anywhere to seek. Shows the A-B span and its markers. |
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
actually is.

```
MyVideo_clip_01m23.4s_to_01m45.9s.mp4
MyVideo_frame_01m23.4s.png
```

**Clips** are cut frame-accurately at your markers — ffmpeg seeks to the preceding
keyframe, decodes forward and re-encodes (H.264 CRF 18 + AAC 192k, `+faststart`),
so the clip begins exactly where marker A is rather than snapping backwards to a
keyframe. A few seconds of work for a short clip. Export runs on a worker thread
with a live percentage, so the UI keeps playing throughout.

**Screenshots** are the decoded source frame at native resolution — a still from a
4K video is 3840×2160 no matter how small the window is — with no controls baked in.

---

## Layout

```
app.py               entry point
videotrim/
  main.py            window, wiring, shortcuts, drag & drop
  player.py          QMediaPlayer wrapper + all A-B loop semantics
  canvas.py          video painting, frosted panel, gestures, toasts
  controls.py        the floating control bar
  scrubber.py        seek bar with A-B rendering
  icons.py           vector glyphs and the glass buttons
  exporter.py        ffmpeg clip export + PNG stills
  paths.py           Desktop resolution, filename safety
  theme.py           palette, metrics, time formatting
```

Playback state lives in `player.py` and the UI observes it through signals, so new
controls generally mean adding a button in `controls.py` and a method in
`player.py` — the natural seam for the load/save and playlist features to come.

## Notes

- Frames are painted by the app rather than handed to a native video widget. That
  is what lets the control panel genuinely blur the video behind it and what makes
  full-resolution stills available instantly. The cost is CPU: 1080p is
  comfortable, very high-bitrate 4K may drop frames on a slow machine.
- Codec support comes from Qt Multimedia's bundled FFmpeg backend, so the common
  formats (MP4/MKV/MOV/AVI/WebM, H.264/HEVC/VP9/AV1) play without a codec pack.
- Built and tested on Python 3.11 with PySide6 6.11.
