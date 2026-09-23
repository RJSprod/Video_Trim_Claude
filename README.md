# Video Trim

A touch-friendly media app for your own machine and the devices around it. Sign
in, and Home offers the tools:

- **Video Trim** — mark a VLC-style A-B loop, watch it repeat, then save that
  exact span as a clip, save just its sound as an MP3, or grab a full-resolution still.
- **Media Transfer** — send photos and videos from a phone or laptop to the
  host's save folder.
- **Files** — browse the save folder from any signed-in device: icons or
  details, sorted how you like, pictures full-screen, and a video opens straight
  into the trimmer.
- **Settings** — account, save location, and which devices may write to this
  machine. Host-only, and only shown on the host.

Controls float on translucent glass that blurs what is behind them, sized for
thumbs, and get out of the way when you're watching.

It comes two ways, sharing one engine:

| | |
| --- | --- |
| **Web app** | In your browser on **port 7862**, reachable from your other devices. One-click install into a local `venv`. |
| **Desktop app** | The original PySide6 window. `python app.py` |

Both cut clips with the same ffmpeg command, and both save through the same
create-only gateway described under [What it protects](#what-it-protects).

---

# What it protects

Video Trim treats everything outside its own install directory as somebody
else's data. Outside that directory it can:

- **read** files you explicitly pick, and list folder names;
- **create brand-new files** in the one save folder you chose.

That is the entire list. It cannot modify, overwrite, truncate, append to,
rename, move, delete or execute any file that was already on your machine. Not
with a flag, not as an administrator, not through any setting — those rules sit
above every feature and no permission can widen them.

Concretely:

- Renders go into the app's own `cache/`, never straight into your save folder,
  so ffmpeg is never pointed at a file of yours.
- The final copy uses an **exclusive create** anchored to a directory handle. If
  the name is taken, the create fails — there is no "check, then write" window
  in which the answer could change.
- A save folder that overlaps the install directory is refused, because the
  app's own cleanup would then be able to delete your saved files.
- Filenames are **rejected**, not quietly rewritten: path separators, control
  characters, colons (an NTFS alternate data stream would otherwise modify an
  existing file straight through the approved path), Windows device names like
  `CON.mp4`, trailing dots and spaces.
- ffmpeg is only ever run from inside this installation — never from your
  `PATH` — with `shell=False` and `-protocol_whitelist file,pipe`, so a crafted
  video cannot make it read other files or reach the network.

There is exactly one place in the codebase that can remove an external file: the
save routine's own failure path, and only when it still holds the descriptor for
a file **it created seconds ago and never finished writing**. A file that
survives a crash is reported to you in Settings and never touched. `python
security/check_fs_calls.py` proves those statements against the source on every
commit.

## What it does not protect against

- **Proving the server's identity on first contact.** Serving the network is
  HTTPS by default, so a passive sniffer sees nothing useful. But the certificate
  is one Video Trim generated itself, and no public authority vouches for it:
  the first time a device connects, its browser warns, and accepting that
  warning is a leap of faith. Compare the fingerprint the launcher prints before
  you accept, or supply a certificate your devices already trust — see
  [HTTPS](#https).
- **Traffic with `--http`.** Asking for plain HTTP means the session cookie and
  your media cross the network in the clear, and anyone able to capture traffic
  on the network segment can take over a session. `HttpOnly` and `SameSite` do
  nothing about a passive sniffer.
- **Device identity.** Write permission follows an *IP address*. If DHCP moves a
  device to a new address it needs allowing again, and devices behind one NAT
  address share one permission. It is LAN convenience control, not device
  authentication.
- **A compromised host.** Everything above is enforced by this application.
  Malware already running as your user is not constrained by it.
- **Whoever is sitting at the machine.** Host-local access is trusted by design.
- **Bugs in ffmpeg, Gradio, uvicorn or your OS.** The architecture keeps the
  reachable surface small; it does not patch dependencies.

---

# The web app

## One-click install

Double-click the launcher for your platform:

| Platform | Launcher |
| --- | --- |
| Windows | `start_windows.bat` |
| Linux | `./start_linux.sh` |
| macOS | `./start_macos.sh` |

The first run creates a **`venv` folder inside this directory** and installs every
dependency into it, then opens <https://127.0.0.1:7862> in your browser. Later runs
see the venv is already good and start straight away.

It is reachable from your other devices out of the box — no flags — and it is
**HTTPS** out of the box too. The launcher prints the address to use:

```
  Video Trim
  ├─ on this machine   https://127.0.0.1:7862
  ├─ from elsewhere    https://192.168.1.42:7862
  ├─                   …everyone must sign in, and a new device cannot save anything
  ├─                   until you allow its address in Settings.
  ├─                   Use --local-only to keep it to this machine.
  ├─ saving to         C:\Users\you\Pictures\Video Trim
  ├─ ffmpeg            C:\Video_Trim_Claude\venv\...\ffmpeg.exe
  ├─ transport         HTTPS (managed self-signed certificate)
  ├─ certificate       data/tls/videotrim.crt (reused)
  ├─ fingerprint       SHA-256 9D:2E:38:C9:E2:0D:ED:8D:91:93:B5:46:F0:11:32:BD
  ├─                           63:DC:92:69:02:20:80:EC:E1:C0:9B:B5:4C:9F:A8:A1
  └─ browser trust     Video Trim generated its own certificate to encrypt this
                       connection. Browsers do not automatically trust locally
                       generated certificates, so each device — this one included —
                       may show a warning the first time. Video Trim does not modify
                       any device's trust store.
```

Open that second URL on a phone or laptop, get past the one-time certificate
warning (see [HTTPS](#https)), and sign in. Marking an A-B loop and saving a clip
puts it in the **host's** save folder — the machine running the launcher — once
you have allowed that device. See [From another machine](#from-another-machine)
for what that does and doesn't allow.

Nothing is installed system-wide and nothing is written outside this folder, so
deleting the folder uninstalls everything. All you need beforehand is Python 3.9+
on `PATH`; the launcher checks and tells you where to get it if not.

```
Video_Trim_Claude/
  venv/            every dependency lives here          (created for you)
  data/            credentials, settings, IP history,
                   and the HTTPS certificate in data/tls/ (created for you)
  cache/           uploads, staging, transcoded previews (created for you)
  CMD_FLAGS.txt    flags applied to every launch
```

`data/` deliberately sits outside `venv/`, so `--recreate` rebuilds the
dependencies without losing your login, your save location or your allowed
devices. Never put a password in `CMD_FLAGS.txt` — it is plain text, and Video
Trim does not read credentials from it.

To reinstall dependencies after pulling new code, run `update_wizard_windows.bat`
(or `./update_wizard_linux.sh`, `./update_wizard_macos.sh`). Add `--recreate` to
throw the venv away and build it from scratch.

## Port 7862

The app is served on **port 7862** on every interface, and that port is treated
as reserved: if something else already holds it, the launcher stops and tells you
what to look for rather than quietly moving to 7863 and leaving you on a dead URL.
Pass `--any-port` if you would rather it take the next free one.

## Flags

Put them after the launcher (`./start_linux.sh --local-only`) or one per line in
`CMD_FLAGS.txt` to apply them every time.

| Flag | Effect |
| --- | --- |
| `--local-only` | Bind `127.0.0.1` only, so nothing outside this machine can reach it. Served over plain HTTP, since loopback traffic never leaves the machine. |
| `--listen-port 7862` | Serve on a different port. |
| `--listen-host 192.168.1.5` | Bind one specific interface. It goes into the certificate as-is. |
| `--output-dir <folder>` | Set the save folder. Must already exist, and must sit outside this installation. Stored, so it is only needed once. |
| `--http` | Serve plain, **unencrypted** HTTP instead of HTTPS. An explicit choice — Video Trim never falls back to it on its own. |
| `--tls-keyfile <key.pem>` `--tls-certfile <cert.pem>` | Use your own certificate instead of the generated one. Both together; PEM; the key without a passphrase. Only ever read. |
| `--share` | Also expose a temporary public `gradio.live` URL. Host Settings are hidden while a tunnel is up, because tunnelled traffic cannot be told apart from this machine's own. The tunnel can only reach plain HTTP, so it needs `--local-only` (or `--http`). |
| `--no-browser` | Don't open a browser window on start. |
| `--any-port` | Use the next free port instead of stopping when 7862 is busy. |
| `--allow-remote-files` | Also let signed-in visitors from other machines browse paths on **this** machine. Reading only — never Settings, credentials, the save location, or write permission. |
| `--proxy-height 720` | Height of the preview built for codecs the browser can't play. |
| `--update` / `--recreate` / `--desktop` | Installer actions — see below. |
| `--change-auth` | Set a new username and password, then exit. |

## HTTPS

Serving your network means HTTPS, with nothing to set up. On the first launch
Video Trim generates its own key and certificate in `data/tls/` and reuses them
from then on. The session cookie is then marked `Secure` and neither it nor your
media crosses the network in the clear.

**The browser warning.** The certificate is *self-signed*: it encrypts the
connection, but no public authority vouches for it, so each browser warns the
first time — *"Your connection is not private"* or similar, on this machine too.
That is expected. To be sure the warning is about *this* certificate, open the
browser's certificate details and compare its SHA-256 fingerprint with the one
the launcher printed, then proceed. Video Trim never installs anything into your
operating system's or browser's trust store to hide the warning.

**When it is plain HTTP instead.** Only when you ask for it with `--http`, or
with `--local-only`, where the app listens on this machine alone and the traffic
never leaves it. If HTTPS cannot be set up — a permission problem in `data/`, a
missing `cryptography` package — the launcher stops and says why. It never
quietly carries on over HTTP.

**When the certificate changes.** Each new certificate brings the warning back on
every device, so it is replaced only when it has to be: it is damaged, it
expires within 30 days, or it doesn't name the address the launcher leads with
(your machine moved to another network, say). Addresses that come and go —
virtual adapters, VPNs — are added whenever a certificate is made, but never
force a new one; the launcher lists any it doesn't cover. Every name an old
certificate covered carries over into the next, so moving between two networks
settles on one certificate. To start over, delete `data/tls/` and restart.

**Your own certificate.** `--tls-keyfile` and `--tls-certfile` use a key and
certificate you already have — from your own CA, or [mkcert](https://github.com/FiloSottile/mkcert)
once its root is installed on your devices, which makes the warning go away. They
must be PEM, the certificate file must start with the server certificate, and the
key must not have a passphrase. Video Trim only reads them: it never rewrites,
renames or changes the permissions of either. The launcher warns if the
certificate has expired or doesn't name the addresses it prints.

**`--share`.** Gradio's tunnel can only reach a plain-HTTP server, so `--share`
is refused alongside HTTPS rather than printing a public link that could never
load. Use `--share --local-only`: the public link is still HTTPS, and nothing on
your network is unencrypted.

**If a device plays nothing.** Some mobile browsers hand video to a system player
that does not honour the warning you accepted for the page. If pages load but
video will not play on a device, give it a certificate it trusts (above), or
use `--http` for that session.

## Opening a video

Three ways, all landing in the same player:

- **Paste a path** into the box at the top and press Enter — a path on the machine
  running the server. Nothing is copied, so this is instant even for a 4K file.
- **Browse…** opens a file picker that walks that machine's folders, the web app's
  stand-in for the desktop app's Open dialog.
- **Drag a file onto the player**, or click *Choose a video…*. The file plays
  from your own device and is **not** sent anywhere — see below.

## What actually gets sent

A video you pick from the device you're holding plays locally, from a blob URL.
Marking A-B, scrubbing, frame stepping and repeat all work on a file the host
has never seen, so a 4 GB source opens instantly instead of after a long upload
you might not have wanted in the first place.

From there:

| You do this | What leaves your device |
| --- | --- |
| Play, mark A-B, scrub, step frames | Nothing |
| **Screenshot** | Just the PNG. Captured in the browser at the source's own pixel size. |
| **Save clip** / **Save audio** | The video, at that moment — because cutting needs ffmpeg. |

So if you only ever take stills, the video never travels. The clip is the one
case that needs the source on the host, and it is sent when you press the button
rather than when you open the file.

Two things follow from playing locally. There is no server-side probe, so the
frame rate is measured from the frames themselves — the first second or so of
playback settles it, and stepping falls back to 25 fps until then, exactly as it
does for a file that reports no rate. And if your browser cannot decode the file
at all (MKV and HEVC are the usual culprits), there is nothing to play locally,
so it is sent to the host and the player says why.

## Signing in

Setup asks for a username and password before it will start anything, and there
is no way to run without them. They are stored in `data/app.db` as an Argon2id
hash — never in plain text, never in `CMD_FLAGS.txt`, never in a log line — and
they survive updates and `--recreate`. To change them later:

```
python one_click.py --change-auth
```

Everything except the login page itself requires a session. That is enforced by
one guard wrapped around the whole application rather than route by route, so
the pages Gradio contributes are covered by the same rule as ours; there is a
test that fails the build if one of them ever becomes reachable.

Changing your password signs every other device out immediately.

## From another machine

The app is served to your whole network by default, so a phone, tablet or laptop
can open it. What a visitor gets is deliberately narrow:

- **They must sign in.** Failed attempts are throttled per address, and the app
  never says whether a username exists.
- **A new device cannot write anything.** Sign-in is not permission. Uploading,
  trimming and transferring all need the host to allow that device's address,
  from the host. Until then the device sees *"File transfer disabled by host"*
  and a **Request access** button, which sets a flag the host sees in Settings
  and grants nothing by itself. There is no remote approval path.
- **They never learn a path on your machine.** Where files land is shown as a
  name you choose ("the host's save folder" by default). No response body, error
  message, toast, tooltip or job label carries a host path, drive letter or
  username to a device that isn't the host.
- **Reading paths on the host does not travel.** "Open this path" and "list this
  folder" are refused for anyone but the machine running the server. Opening the
  LAN URL *on the host* still counts as local — the host's own addresses are
  recognised. `--allow-remote-files` lifts that for reading and **only** reading:
  it never grants Settings, credentials, the save location, write permission, or
  access to media somebody else opened.

To allow a device, open **Settings → Connected devices** on the host. Pending
requests are pinned at the top with the address, the username that signed in and
the time, so you can match it to the phone in your hand. The switch takes effect
on the next request — no restart. Turning it off cancels that device's in-flight
work.

`--local-only` restores loopback-only binding if you would rather nothing else
could reach it at all (and serves plain HTTP, since nothing leaves the machine).

Uploads are kept in `cache/uploads` and swept when they are over a day old
(anything still open in a session is never swept). An upload is refused up front
if it clearly won't fit in the host's free disk space.

## Media Transfer

Home → **Media Transfer** sends photos and videos from the device you are
holding to the host's save folder. Files stream to the host rather than being
buffered in memory, so a multi-gigabyte video is fine.

Progress is reported in two phases on purpose — *Sending* and then *Finalizing*
— because the upload finishing is not the same as the file being saved.

**A name that already exists is skipped.** Nothing is replaced and no
`name (2).jpg` is invented; you get *"Already exists — skipped"* and the file
you already had is untouched. (Generated Video Trim clips do get a new name
instead, because those are output the app made up rather than a file you named.)

One consequence worth stating: a device you have allowed can learn which
filenames exist in the save folder by trying them and reading the answer. It is
signed in, you approved it, and the folder is the one you designated to receive
its files — so this is accepted rather than papered over with vague errors that
would make legitimate skips unreadable.

## Files

Home → **Files** is a read-only window onto the folder the host chose in
Settings — whatever that folder is, this is what the web UI can see, from the
host's own browser or from a phone across the room.

| | |
| --- | --- |
| **Views** | Details, or icons at four sizes. |
| **Details columns** | Name is always there; Type, Size, Date modified, Extension and Category can be switched on or off, and every column sorts. |
| **Sorting** | By any column, ascending or descending, from the sort control or by clicking a column heading. Folders always lead. |
| **Filters** | All, Folders, Videos, Pictures, Audio, Other — with a count each. |
| **Pictures** | Open full-screen, step through the folder's other pictures with ← and →, download the one on screen. |
| **Videos** | Open into the player — the same A-B loop, the same export, the same gear — with **← Back to files** returning you to the listing you left. |

Your choice of view, sort order, filters and columns is remembered in the
browser, per device.

What it can and cannot do is worth being precise about, because this is the one
feature that lets a browser name a file at all:

- Everything is named **relative to the save folder**. A session that is not the
  host's own is never told the folder's real path — not in a listing, not in an
  error, not anywhere.
- `..`, drive letters, colons and symlinks pointing out of the folder are
  refused rather than tidied up, so nothing outside the folder can be reached.
- Pictures and videos are served; anything else is listed with its size and type
  but its contents are not handed out.
- **It reads.** There is no rename, no delete, no move, no overwrite and no
  upload behind this page — the same create-only rule the rest of the app lives
  under, minus even the create.

Video posters are rendered once by ffmpeg into `cache/thumbs` and reused, keyed
by the file's size and modification time, so a replaced file gets a new poster.

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

The web app's venv deliberately doesn't include PySide6 — it's a large download the
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

The web app takes the same argument: `python webui.py "C:\...\clip.mp4"` opens the
browser straight into that file.

---

# Using it

Everything below is true of both front ends: the web app reproduces the desktop
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
| Save video (frame + ↓) | Saves the current A-B range as a video to the host's save folder. Disabled until both markers exist. |
| Save audio (note + ↓) | Saves only the sound of the A-B range, as a stereo MP3 at 192 kbps. Disabled until both markers exist. The web app only. |
| ⛶ Screenshot | Saves the frame on screen there as a PNG. |
| ⚙ Export options | Frame size, compression, encoder speed, frame rate and audio, with a running size estimate. Web app only — see below. |
| 🔊 Mute | Toggles audio. |
| 🗀 Open | Open a different video without leaving the session. |

You can also drag a video file onto the window at any time.

In the web app the same bar sits over the video as a real backdrop-blurred panel,
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

### Export options

In the web app the control bar carries a **gear**. It is in the bar rather than
beside the video on purpose: it appears with the controls, goes away with them,
and cannot be left open over a video you are watching.

| Option | What it changes |
| --- | --- |
| **Frame size** | 100 / 75 / 50 / 33 / 25 %, or a width you type. Height follows — the ratio is locked to the source's, and nothing is ever scaled *up*. |
| **Compression** | The x264 CRF, 14 (near-lossless) to 34 (small and soft). 18 is the default, and what every earlier version cut at. |
| **Encoder speed** | ultrafast → slower. Slower spends longer looking for savings and lands a smaller file at the same quality. |
| **Frame rate** | A cap: same as source, or 60 / 30 / 24 / 15. |
| **Audio** | 256 / 192 / 128 / 96 kbps AAC, or none at all. |

Under them is the number the whole menu exists for:

> **Whole video ≈ 412 MB**  ·  A-B range ≈ 41 MB

The first line is the export at these settings **for the entire file** — A at the
very start, B at the very end — so it is the largest a clip of this video can
come out at. The second is what the range you have marked would cost.

The estimate is a bits-per-pixel-per-frame model of x264's rate control, halving
per six points of CRF, adjusted for the preset and the audio bitrate. It lives in
`videotrim/encoding.py` and the page is handed its constants rather than a copy
of the arithmetic, so the figure under the slider and the encode that runs are
described by the same numbers. It is still an estimate: a still blue sky and a
minute of confetti do not encode to the same size at the same CRF.

Choices are remembered in the browser, and the gear wears a dot whenever they
are no longer the defaults. They apply to **clips**; stills are always saved at
the source's own resolution. Whatever the page asks for, the server re-derives
the frame size from the file it probed and clamps every value again — a browser
can request a width, never a height, and never one larger than the source.

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
| | | | `A` | Save the A-B audio as MP3 (web) |
| `Home` | Stop (back to A) | | `F` / `F11` | Fullscreen |
| `O` / `Ctrl+O` | Open a video | | `G` | Export options (web) |
| | | | `Esc` | Close the options, or leave fullscreen |

---

## Where saves go

You choose the folder, and there is no default. Open **Settings → Save location**
on the host and pick one, or pass `--output-dir <folder>` once and it is stored.

Three things follow from that, and all three are deliberate:

- The folder **must already exist**. Video Trim never creates a folder outside
  its own directory.
- If it later disappears — an unmounted drive, a renamed folder — saving fails
  and says so. It never quietly falls back to your Desktop or your home folder.
- It **cannot be inside the Video Trim installation**. Choosing one gets you
  *"That folder is inside the Video Trim installation. Choose a folder outside
  it, so the app's own cleanup can never touch your saved files."*

> **Upgrading?** Saves used to go to your Desktop automatically. They no longer
> do, and the desktop app reads the same setting — so both refuse to export
> until you have chosen a folder once. This is a one-time step, not a bug.

Outputs are auto-named from the source file and the timecode, and **never
overwrite**: if the generated name is taken, the app creates `… (2)` instead of
touching what is there.

Files land on the machine running the server, because that is where ffmpeg runs
— the browser only drives the UI. Open it on your own machine and that is your
folder; save from a phone across the network and it still lands on the host,
with a download link offered under the video if you want a copy on the phone.
The host sees the real path; other devices see the name you chose for it.

If a save is interrupted mid-copy, the app removes the file it was writing — but
only while it can still prove it created it. After a crash it cannot prove that,
so the file stays, and **Settings → Maintenance notices** tells you the name so
you can check it yourself. Video Trim will not delete, repair or overwrite it.

```
MyVideo_clip_01m23.4s_to_01m45.9s.mp4
MyVideo_audio_01m23.4s_to_01m45.9s.mp3
MyVideo_frame_01m23.4s.png
```

**Clips** are cut frame-accurately at your markers — ffmpeg seeks to the preceding
keyframe, decodes forward and re-encodes (H.264 CRF 18 + AAC 192k, `+faststart`),
so the clip begins exactly where marker A is rather than snapping backwards to a
keyframe. A few seconds of work for a short clip. Export runs on a worker thread
with a live percentage, so the UI keeps playing throughout. Both front ends build
that ffmpeg command from the same function, so a clip cut in the browser is the
same job as one cut in the window.

**Audio only** uses the same markers and the same frame-accurate cut, and saves
the first audio track as an MP3: always **stereo, 192 kbps constant bitrate,
48 kHz** — about 1.4 MB a minute. A mono track plays on both channels and
surround is folded down to stereo. A video with no sound says so rather than
saving an empty file. The gear menu's options are for the picture and don't
apply here.

**Screenshots** are the source frame at native resolution — a still from a 4K video
is 3840×2160 no matter how small the window is — with no controls baked in. The
desktop app saves the frame it already decoded. In the browser it depends on where
the file is: one you picked from your own device is captured on a canvas and only
the PNG is sent, while one already on the host is captured there by ffmpeg at that
timestamp. Same pixels, same size, either way.

---

## Layout

```
app.py               desktop entry point
webui.py             web app entry point — argument parsing, port 7862, uvicorn
one_click.py         builds venv/ and launches; the launchers all call this
start_*.sh|bat       one-click launchers per platform
update_wizard_*      reinstall dependencies into the existing venv
CMD_FLAGS.txt        flags applied to every launch

security/
  fs_allowlist.toml  every mutating filesystem call, with a justification
  check_fs_calls.py  CI gate — fails the build on any call not listed
tests/security/      the P0 suites and their fixture host tree

videotrim/
  ── the host-protection layer, used by both front ends ──
  security/fs_boundary.py   read / create-only policy, the exclusive-create
                            gateway, the name sanitizer, zone separation
  security/auth.py          Argon2id credentials, sessions, CSRF
  security/network.py       IP normalisation; host-admin vs browse capability;
                            this machine's own addresses
  security/tls.py           HTTPS: the managed certificate in data/tls/, or
                            a supplied one; what each page says about it
  security/write_policy.py  per-IP write authorization
  security/middleware.py    the default-deny guard around the whole app
  config/store.py           data/app.db — credentials, settings, IP history,
                            and the interrupted-run journal
  config/settings.py        typed settings, including the save location

  ── shared by both front ends, no Qt imports ──
  ffmpeg_tools.py    ffmpeg discovery, probing, clip/still/preview/poster
                     commands
  encoding.py        export options: what may be asked for, and how big the
                     result will be
  naming.py          output filenames
  paths.py           name formatting (no longer a security boundary)
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

  ── web app ──
  web/server.py      routes, the Gradio mount, and the guard around both
  web/shell.py       tool registry, capabilities, and the pages
  web/transfer.py    Media Transfer
  web/library.py     Files — the read-only save-folder browser
  web/admin.py       host-only Settings
  web/output.py      the single way anything leaves this process
  web/media.py       session-scoped token registry, Range-aware streaming
  web/jobs.py        background ffmpeg jobs with pollable progress
  web/assets/        login.*, shell.*, player.* and files.* — the front end
```

In the desktop app, playback state lives in `player.py` and the UI observes it
through signals, so new controls generally mean adding a button in `controls.py`
and a method in `player.py`.

The web app is the same shape with the seam moved: `player.js` owns the A-B rules
(a direct port of `player.py`'s), Gradio provides the tool page and the source
toolbar, and the `/vt` routes do the work only the host can do — reading files
and running ffmpeg. Because both front ends call into `ffmpeg_tools.py`, changing
how a clip is cut changes it in both.

Feature code never touches the host filesystem directly. It renders into
`cache/` and calls `web/output.py`, which is the only caller of the gateway in
`security/fs_boundary.py`. That is what makes the promise checkable rather than
a matter of everyone remembering: `security/check_fs_calls.py` walks the source
and fails on any mutating call that is not accounted for, including a second
external delete appearing anywhere.

## Notes

- **Desktop app:** frames are painted by the app rather than handed to a native
  video widget. That is what lets the control panel genuinely blur the video behind
  it and what makes full-resolution stills available instantly. The cost is CPU:
  1080p is comfortable, very high-bitrate 4K may drop frames on a slow machine.
  Codec support comes from Qt Multimedia's bundled FFmpeg backend, so the common
  formats (MP4/MKV/MOV/AVI/WebM, H.264/HEVC/VP9/AV1) play without a codec pack.
- **Web app:** decoding is the browser's, which is narrower — hence the preview
  transcode for files it won't take. Video is streamed with byte-range requests so
  scrubbing a large file doesn't wait on a download, and files are addressed by
  opaque token rather than by path. Each token belongs to the session that made
  it: two people opening the same file get two tokens, and a token dies with its
  session, so one cannot be replayed by another device to read something you
  opened. It binds every interface by default so other devices can reach it; the
  routes that read host paths check the requesting address and refuse anything
  that isn't the host itself.
- **Running the checks:**

  ```
  python security/check_fs_calls.py     # the static review gate
  python -m pytest tests/security -q    # the P0 suites
  ```

  Both also run in CI. The suites build a fake install root next to a tree of
  host files, record their hashes and metadata, and assert after every success,
  failure, cancellation, duplicate, race and malicious name that those files are
  byte-for-byte and stat-for-stat identical.
- Built and tested on Python 3.11, with PySide6 6.11 and Gradio 6. The web app keeps
  to long-stable Gradio API (`Blocks`, `HTML`, event `js=`, `mount_gradio_app`) and
  pins `gradio>=4.44,<7`.
