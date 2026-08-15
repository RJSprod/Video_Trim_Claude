"""The translucent control bar that floats over the bottom of the video.

The widget paints no background of its own -- VideoCanvas draws the frosted
glass panel underneath it so the blur samples the live video frame.
"""

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QSizePolicy, QVBoxLayout, QWidget

from . import theme
from .icons import ABButton, GlassButton
from .scrubber import Scrubber
from .timefmt import fmt_time


def _label(dim=False, bold=False):
    label = QLabel()
    colour = theme.TEXT_DIM if dim else theme.TEXT
    weight = "600" if bold else "500"
    label.setStyleSheet(
        f"color: rgba({colour.red()},{colour.green()},{colour.blue()},{colour.alpha()});"
        f" font-weight: {weight}; background: transparent;"
    )
    label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
    return label


class ControlBar(QWidget):
    interacted = Signal()
    openRequested = Signal()
    playPauseRequested = Signal()
    stopRequested = Signal()
    skipRequested = Signal(int)
    stepRequested = Signal(int)
    repeatToggled = Signal(bool)
    markerCycled = Signal()
    saveClipRequested = Signal()
    screenshotRequested = Signal()
    muteToggled = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        self.setAutoFillBackground(False)

        self.scrubber = Scrubber(self)

        self.btn_stop = GlassButton("stop", "Stop — back to the start of the loop", self)
        self.btn_rew = GlassButton("rewind", "Back 5 seconds  (←)", self, badge="5")
        self.btn_step_back = GlassButton("step_back", "Previous frame  (,)", self)
        self.btn_play = GlassButton("play", "Play  (Space)", self, primary=True)
        self.btn_step_fwd = GlassButton("step_forward", "Next frame  (.)", self)
        self.btn_fwd = GlassButton("forward", "Forward 5 seconds  (→)", self, badge="5")
        self.btn_repeat = GlassButton("repeat", "Repeat  (R)", self)
        self.btn_repeat.setCheckable(True)

        self.btn_ab = ABButton(self)
        self.btn_clip = GlassButton("save", "Save the A-B clip to the Desktop  (C)", self)
        self.btn_shot = GlassButton("camera", "Save this frame to the Desktop  (S)", self)
        self.btn_mute = GlassButton("volume", "Mute  (M)", self)
        self.btn_mute.setCheckable(True)
        self.btn_open = GlassButton("folder", "Open another video…  (Ctrl+O)", self)

        self.lbl_time = _label(bold=True)
        self.lbl_time.setText("0:00 / 0:00")
        self.lbl_loop = _label(dim=True)

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(4)

        times = QVBoxLayout()
        times.setContentsMargins(0, 0, 0, 0)
        times.setSpacing(0)
        times.addWidget(self.lbl_time)
        times.addWidget(self.lbl_loop)
        time_box = QWidget(self)
        time_box.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        time_box.setLayout(times)
        time_box.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)
        row.addWidget(time_box, 0, Qt.AlignVCenter)

        row.addStretch(1)
        centre = (
            self.btn_stop,
            self.btn_rew,
            self.btn_step_back,
            self.btn_play,
            self.btn_step_fwd,
            self.btn_fwd,
            self.btn_repeat,
        )
        for button in centre:
            row.addWidget(button, 0, Qt.AlignVCenter)
        row.addStretch(1)

        for button in (self.btn_ab, self.btn_clip, self.btn_shot, self.btn_mute, self.btn_open):
            row.addWidget(button, 0, Qt.AlignVCenter)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 10, 18, 12)
        outer.setSpacing(2)
        outer.addWidget(self.scrubber)
        outer.addLayout(row)

        self.btn_open.clicked.connect(self.openRequested)
        self.btn_play.clicked.connect(self.playPauseRequested)
        self.btn_stop.clicked.connect(self.stopRequested)
        self.btn_rew.clicked.connect(lambda: self.skipRequested.emit(-5000))
        self.btn_fwd.clicked.connect(lambda: self.skipRequested.emit(5000))
        self.btn_step_back.clicked.connect(lambda: self.stepRequested.emit(-1))
        self.btn_step_fwd.clicked.connect(lambda: self.stepRequested.emit(1))
        self.btn_repeat.toggled.connect(self.repeatToggled)
        self.btn_ab.clicked.connect(self.markerCycled)
        self.btn_clip.clicked.connect(self.saveClipRequested)
        self.btn_shot.clicked.connect(self.screenshotRequested)
        self.btn_mute.clicked.connect(self.muteToggled)

        # Touching anything on the bar counts as use, so it does not time out
        # from under the user mid-interaction.
        for button in centre + (self.btn_ab, self.btn_clip, self.btn_shot, self.btn_mute, self.btn_open):
            button.pressed.connect(self.interacted)
        self.scrubber.scrubbing.connect(lambda *_: self.interacted.emit())

        self._duration = 0
        self._position = 0
        self._scrub_preview = None
        self.scrubber.scrubbing.connect(self._on_scrubbing)

    def mousePressEvent(self, event):
        # Swallow presses that land on the bar's own background. Without this
        # they propagate up to the canvas and are read as a tap on the video,
        # dismissing the bar the user was reaching into.
        self.interacted.emit()
        event.accept()

    # --- state sync ----------------------------------------------------------
    def set_playing(self, playing):
        self.btn_play.set_glyph("pause" if playing else "play")
        self.btn_play.setToolTip("Pause  (Space)" if playing else "Play  (Space)")

    def set_duration(self, ms):
        self._duration = ms
        self.scrubber.set_duration(ms)
        self._refresh_time()

    def set_position(self, ms):
        self._position = ms
        self.scrubber.set_position(ms)
        self._refresh_time()

    def set_markers(self, a, b):
        self.scrubber.set_markers(a, b)
        state = 0 if a is None else (2 if b is not None else 1)
        self.btn_ab.set_state(state)
        self.btn_clip.setEnabled(state == 2)
        if state == 2:
            span = fmt_time(b - a, tenths=True)
            self.lbl_loop.setText(f"A-B  {fmt_time(a)} → {fmt_time(b)}   ({span})")
        elif state == 1:
            self.lbl_loop.setText(f"A  {fmt_time(a)} — tap A-B again to set B")
        else:
            self.lbl_loop.setText("")
        self._refresh_time()

    def set_repeat(self, enabled):
        self.btn_repeat.blockSignals(True)
        self.btn_repeat.setChecked(enabled)
        self.btn_repeat.blockSignals(False)

    def set_muted(self, muted):
        self.btn_mute.setChecked(muted)
        self.btn_mute.set_glyph("mute" if muted else "volume")
        self.btn_mute.setToolTip("Unmute  (M)" if muted else "Mute  (M)")

    def set_media_loaded(self, loaded):
        for button in (
            self.btn_stop,
            self.btn_rew,
            self.btn_step_back,
            self.btn_play,
            self.btn_step_fwd,
            self.btn_fwd,
            self.btn_repeat,
            self.btn_ab,
            self.btn_shot,
            self.btn_mute,
        ):
            button.setEnabled(loaded)
        if not loaded:
            self.btn_clip.setEnabled(False)

    def _on_scrubbing(self, active, preview):
        self._scrub_preview = preview if active else None
        self._refresh_time()

    def _refresh_time(self):
        shown = self._scrub_preview if self._scrub_preview is not None else self._position
        self.lbl_time.setText(f"{fmt_time(shown, tenths=True)} / {fmt_time(self._duration)}")
