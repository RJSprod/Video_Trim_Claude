"""Playback engine: wraps QMediaPlayer and owns the A-B loop semantics.

The A-B rules, matching VLC:

* tap 1 sets marker A, tap 2 sets marker B, tap 3 clears both;
* once both markers exist every form of playback is confined to ``[A, B]``;
* Stop returns to A (or 0 when no loop is set);
* Repeat loops the A-B range, or the whole file when no range is set.
"""

from PySide6.QtCore import QObject, QTimer, QUrl, Signal
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer, QVideoSink

# Shortest span we accept between A and B. Anything tighter is a mis-tap.
MIN_LOOP_MS = 120

# After issuing a loop seek, ignore boundary checks for this long so the
# in-flight seek does not retrigger the loop.
SEEK_GUARD_MS = 260

SKIP_MS = 5000


class PlayerController(QObject):
    positionChanged = Signal(int)
    durationChanged = Signal(int)
    playingChanged = Signal(bool)
    markersChanged = Signal(object, object)  # a_ms, b_ms (either may be None)
    repeatChanged = Signal(bool)
    mutedChanged = Signal(bool)
    sourceChanged = Signal(str)  # filesystem path, "" when cleared
    errorRaised = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._player = QMediaPlayer(self)
        self._audio = QAudioOutput(self)
        self._audio.setVolume(0.85)
        self._player.setAudioOutput(self._audio)

        self.sink = QVideoSink(self)
        self._player.setVideoSink(self.sink)

        self._path = ""
        self._a = None
        self._b = None
        self._repeat = False
        self._priming = False
        self._guard = 0

        # Polls the boundary while playing; positionChanged alone is too coarse
        # to land a tight loop cleanly.
        self._tick = QTimer(self)
        self._tick.setInterval(25)
        self._tick.timeout.connect(self._enforce_bounds)

        self._player.positionChanged.connect(self._on_position)
        self._player.durationChanged.connect(self._on_duration)
        self._player.playbackStateChanged.connect(self._on_state)
        self._player.mediaStatusChanged.connect(self._on_status)
        self._player.errorOccurred.connect(self._on_error)

    # --- state ---------------------------------------------------------------
    @property
    def path(self):
        return self._path

    @property
    def markers(self):
        return self._a, self._b

    @property
    def has_loop(self):
        return self._a is not None and self._b is not None

    @property
    def marker_state(self):
        if self._a is None:
            return 0
        return 2 if self._b is not None else 1

    @property
    def repeat(self):
        return self._repeat

    @property
    def muted(self):
        return self._audio.isMuted()

    @property
    def duration(self):
        return max(0, self._player.duration())

    @property
    def position(self):
        return max(0, self._player.position())

    @property
    def is_playing(self):
        return self._player.playbackState() == QMediaPlayer.PlayingState

    @property
    def has_media(self):
        return bool(self._path)

    def bounds(self):
        """The window playback is currently allowed to move within."""
        if self.has_loop:
            return self._a, self._b
        return 0, self.duration

    # --- loading -------------------------------------------------------------
    def open(self, path):
        path = str(path)
        self._tick.stop()
        self.clear_markers()
        self._player.stop()
        self._path = path
        self._priming = True
        self._player.setSource(QUrl.fromLocalFile(path))
        self.sourceChanged.emit(path)

    # --- transport -----------------------------------------------------------
    def play(self):
        if not self.has_media:
            return
        start, end = self.bounds()
        pos = self.position
        at_end = self._player.mediaStatus() == QMediaPlayer.EndOfMedia
        if at_end or pos < start or pos >= max(start, end - 40):
            self._seek(start)
        self._player.play()

    def pause(self):
        self._player.pause()

    def toggle(self):
        if self.is_playing:
            self.pause()
        else:
            self.play()

    def stop(self):
        """Pause and return to the start of the active range (A, or 0)."""
        start, _ = self.bounds()
        self._player.pause()
        self._seek(start)

    def skip(self, delta_ms):
        if not self.has_media:
            return
        start, end = self.bounds()
        target = self.position + delta_ms
        target = max(start, min(target, max(start, end - 60)))
        self._seek(target)

    def seek(self, ms):
        """Seek from the UI. Clamped into the A-B range when one is set."""
        start, end = self.bounds()
        self._seek(max(start, min(int(ms), max(start, end - 20))))

    def _seek(self, ms):
        self._guard = SEEK_GUARD_MS
        self._player.setPosition(max(0, int(ms)))

    def set_repeat(self, enabled):
        enabled = bool(enabled)
        if enabled != self._repeat:
            self._repeat = enabled
            self.repeatChanged.emit(enabled)

    def toggle_repeat(self):
        self.set_repeat(not self._repeat)

    def set_muted(self, muted):
        self._audio.setMuted(bool(muted))
        self.mutedChanged.emit(self._audio.isMuted())

    def toggle_mute(self):
        self.set_muted(not self._audio.isMuted())

    def set_volume(self, value):
        self._audio.setVolume(max(0.0, min(1.0, float(value))))

    @property
    def volume(self):
        return self._audio.volume()

    # --- A-B markers ---------------------------------------------------------
    def cycle_marker(self):
        """One tap of the A-B button. Returns a short status string for a toast."""
        if not self.has_media:
            return ""
        pos = self.position
        if self._a is None:
            self._a = pos
            message = "Loop point A set"
        elif self._b is None:
            if abs(pos - self._a) < MIN_LOOP_MS:
                return "Move further along before setting B"
            # Marking "backwards" is treated as re-ordering rather than an error.
            self._a, self._b = min(self._a, pos), max(self._a, pos)
            message = "A-B loop set"
        else:
            self._a = self._b = None
            message = "A-B loop cleared"
        self.markersChanged.emit(self._a, self._b)
        if self.has_loop:
            start, end = self.bounds()
            if not (start <= self.position < end):
                self._seek(start)
        return message

    def clear_markers(self):
        if self._a is not None or self._b is not None:
            self._a = self._b = None
            self.markersChanged.emit(None, None)

    # --- internals -----------------------------------------------------------
    def _on_position(self, pos):
        self.positionChanged.emit(max(0, int(pos)))

    def _on_duration(self, ms):
        self.durationChanged.emit(max(0, int(ms)))

    def _on_state(self, state):
        playing = state == QMediaPlayer.PlayingState
        if playing:
            self._tick.start()
        else:
            self._tick.stop()
        self.playingChanged.emit(playing)

    def _on_status(self, status):
        if status == QMediaPlayer.LoadedMedia and self._priming:
            # Nudge the pipeline so the very first frame is decoded and shown
            # while the player sits paused at 0.
            self._player.play()
            QTimer.singleShot(60, self._finish_priming)
        elif status == QMediaPlayer.EndOfMedia:
            self._priming = False
            self._on_end_of_media()
        elif status == QMediaPlayer.InvalidMedia:
            self._priming = False
            self.errorRaised.emit("That file could not be opened — unsupported or damaged.")

    def _finish_priming(self):
        if not self._priming:
            return
        self._priming = False
        self._player.pause()
        self._player.setPosition(0)

    def _on_end_of_media(self):
        start, _ = self.bounds()
        if self._repeat:
            self._seek(start)
            self._player.play()
        else:
            self._player.pause()
            self._seek(start)

    def _enforce_bounds(self):
        if self._guard > 0:
            self._guard -= self._tick.interval()
            return
        if not self.has_loop or not self.is_playing:
            return
        start, end = self.bounds()
        pos = self.position
        if pos >= end - 30:
            if self._repeat:
                self._seek(start)
            else:
                self._player.pause()
                self._seek(max(start, end - 40))
        elif pos < start - 250:
            self._seek(start)

    def _on_error(self, _error, message=""):
        if message:
            self.errorRaised.emit(message)
