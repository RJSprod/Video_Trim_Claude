"""Export options: what a clip may be re-encoded to, and how big that will be.

Two jobs, deliberately in one module.

The first is *validation*. Everything here arrives from a browser, and every
value ends up as an argument to ffmpeg, so nothing is passed through: each field
is coerced to a number or matched against a fixed tuple, and anything that does
not fit falls back to the default rather than being forwarded. ``normalize()``
is the only thing feature code calls, and what it returns is the only shape
``clip_command()`` accepts.

The second is the *size estimate* the options menu shows while you drag a
slider. The model is a bits-per-pixel-per-frame curve anchored on one measured
point and halved every ``CRF_HALVING`` steps, which is how x264's rate control
actually behaves. It lives here, in Python, and the browser is handed the
constants rather than a copy of the arithmetic — so the number under the slider
and the file that eventually lands come from the same description of the codec.

An estimate is an estimate. A still frame of a blue sky and a minute of confetti
do not encode to the same size at the same CRF, so the UI says "about" and never
promises.
"""

# --- the choices a client may make -------------------------------------------
# x264 speed presets, slowest last. Anything outside this tuple is refused.
PRESETS = (
    "ultrafast", "superfast", "veryfast", "faster", "fast", "medium",
    "slow", "slower",
)

# How much bigger a file gets at the same CRF because the encoder had less time
# to look for savings. Relative to "medium", which is the anchor point below.
PRESET_FACTORS = {
    "ultrafast": 1.65,
    "superfast": 1.35,
    "veryfast": 1.20,
    "faster": 1.10,
    "fast": 1.05,
    "medium": 1.00,
    "slow": 0.93,
    "slower": 0.88,
}

# Constant Rate Factor. Lower is bigger and better; 18 is the app's long-
# standing default and is visually lossless for most material.
CRF_MIN = 14
CRF_MAX = 34

# AAC bitrates, plus 0 meaning "drop the audio track entirely".
AUDIO_CHOICES = (0, 96, 128, 192, 256)

# A cap, never an increase: 0 means "whatever the source runs at".
FPS_CHOICES = (0, 15, 24, 30, 60)

# Scaling bounds. The upper bound is 8K; the lower one keeps a scaled clip from
# becoming a thumbnail by accident.
MIN_WIDTH = 128
MAX_WIDTH = 7680

DEFAULTS = {
    "width": 0,          # 0 = the source's own width; never an upscale
    "crf": 18,
    "preset": "veryfast",
    "fps_cap": 0,
    "audio_kbps": 192,
}

# --- the size model ----------------------------------------------------------
# Bits per pixel per frame at the anchor: CRF 23, preset medium, mixed content.
# 1920x1080 at 30 fps works out to about 4.0 Mbit/s, which is where general
# H.264 encoding of ordinary footage lands.
ANCHOR_BPP = 0.0643
ANCHOR_CRF = 23.0

# x264 file size roughly halves for every six points of CRF added.
CRF_HALVING = 6.0

# MP4 container and muxing overhead, as a fraction of the payload.
CONTAINER_OVERHEAD = 0.005


def _even(value, floor=2):
    """Round to an even number — H.264 with yuv420p cannot encode odd sides."""
    number = int(round(float(value)))
    if number % 2:
        number -= 1
    return max(floor, number)


def _clamp(value, low, high):
    return max(low, min(high, value))


def _int(raw, fallback):
    try:
        return int(round(float(raw)))
    except (TypeError, ValueError):
        return fallback


def normalize(raw, source_width=0, source_height=0):
    """Vet a client's requested options against the source it will be applied to.

    Returns a dict with exactly the keys ``clip_command`` reads. Unknown keys are
    dropped, out-of-range numbers are clamped, and an unrecognised preset becomes
    the default — a refusal here would only turn a stale bookmark into a failed
    export, and every value is bounded anyway.

    Scaling is derived from the *source's* real dimensions, which the server
    probed, rather than from a width and height the client sent as a pair. That
    is what keeps the aspect ratio locked no matter what the page does.
    """
    raw = raw if isinstance(raw, dict) else {}
    options = dict(DEFAULTS)

    options["crf"] = _clamp(_int(raw.get("crf"), DEFAULTS["crf"]), CRF_MIN, CRF_MAX)

    preset = str(raw.get("preset") or "").strip().lower()
    options["preset"] = preset if preset in PRESET_FACTORS else DEFAULTS["preset"]

    audio = _int(raw.get("audio_kbps"), DEFAULTS["audio_kbps"])
    options["audio_kbps"] = audio if audio in AUDIO_CHOICES else DEFAULTS["audio_kbps"]

    fps_cap = _int(raw.get("fps_cap"), DEFAULTS["fps_cap"])
    options["fps_cap"] = fps_cap if fps_cap in FPS_CHOICES else DEFAULTS["fps_cap"]

    source_width = max(0, _int(source_width, 0))
    source_height = max(0, _int(source_height, 0))
    wanted = max(0, _int(raw.get("width"), 0))

    # Without probed dimensions there is nothing to lock an aspect ratio to, so
    # the safe answer is to leave the frame alone.
    if wanted and source_width and source_height:
        width = _even(_clamp(wanted, MIN_WIDTH, min(MAX_WIDTH, source_width)))
        if width >= source_width:
            options["width"] = 0
            options["height"] = 0
        else:
            options["width"] = width
            options["height"] = _even(width * source_height / float(source_width))
    else:
        options["width"] = 0
        options["height"] = 0

    return options


def command_settings(options):
    """Bound an already-derived option set so a command builder can use it.

    ``normalize()`` needs the source's dimensions to lock an aspect ratio;
    this does not, because by the time a command is built the pair has already
    been derived from them. It exists so ``clip_command`` is safe to call
    directly — from a test, from the desktop app, from anywhere — without a
    caller having to remember which shape it wanted.
    """
    options = options if isinstance(options, dict) else {}
    settings = dict(DEFAULTS)
    settings["height"] = 0

    settings["crf"] = _clamp(_int(options.get("crf"), DEFAULTS["crf"]), CRF_MIN, CRF_MAX)

    preset = str(options.get("preset") or "").strip().lower()
    settings["preset"] = preset if preset in PRESET_FACTORS else DEFAULTS["preset"]

    audio = _int(options.get("audio_kbps"), DEFAULTS["audio_kbps"])
    settings["audio_kbps"] = audio if audio in AUDIO_CHOICES else DEFAULTS["audio_kbps"]

    fps_cap = _int(options.get("fps_cap"), DEFAULTS["fps_cap"])
    settings["fps_cap"] = fps_cap if fps_cap in FPS_CHOICES else DEFAULTS["fps_cap"]

    width = max(0, _int(options.get("width"), 0))
    height = max(0, _int(options.get("height"), 0))
    if width and height:
        settings["width"] = _even(_clamp(width, MIN_WIDTH, MAX_WIDTH))
        settings["height"] = _even(_clamp(height, 2, MAX_WIDTH))
    else:
        settings["width"] = 0
        settings["height"] = 0
    return settings


def is_default(options):
    """True when these options describe exactly what the app has always done."""
    options = options or {}
    return all(options.get(key, value) == value for key, value in DEFAULTS.items()) \
        and not options.get("height")


def scale_filter(options):
    """The ``-vf`` value for these options, or "" when the frame is untouched."""
    width = int((options or {}).get("width") or 0)
    height = int((options or {}).get("height") or 0)
    if width <= 0 or height <= 0:
        return ""
    # Explicit both ways: the pair was already derived from the source's aspect,
    # and letting ffmpeg re-derive one side can land a pixel off.
    return f"scale={width}:{height}:flags=lanczos"


def estimate_bytes(width, height, fps, duration_ms, options):
    """About how large an export of ``duration_ms`` at these options will be.

    Deliberately returns the *whole-length* number when handed the whole length:
    the question the options menu answers is "what is the biggest this can get",
    which is the full duration with A at the start and B at the end.
    """
    options = normalize(options if isinstance(options, dict) else {},
                        source_width=width, source_height=height)
    seconds = max(0.0, float(duration_ms or 0)) / 1000.0
    if seconds <= 0:
        return 0

    out_width = options["width"] or int(width or 0)
    out_height = options["height"] or int(height or 0)
    rate = float(fps or 0) or 30.0
    if options["fps_cap"]:
        rate = min(rate, float(options["fps_cap"]))
    if out_width <= 0 or out_height <= 0:
        return 0

    bpp = (
        ANCHOR_BPP
        * (2.0 ** ((ANCHOR_CRF - options["crf"]) / CRF_HALVING))
        * PRESET_FACTORS[options["preset"]]
    )
    video_bits = bpp * out_width * out_height * rate
    audio_bits = options["audio_kbps"] * 1000.0
    total = (video_bits + audio_bits) * seconds / 8.0
    return int(total * (1.0 + CONTAINER_OVERHEAD))


def model():
    """The constants the options menu needs to do this arithmetic live.

    Handed to the browser so the estimate under the slider and the encode the
    server eventually runs are described by the same numbers. If this module's
    curve is retuned, every page picks that up on its next load.
    """
    return {
        "defaults": dict(DEFAULTS),
        "presets": list(PRESETS),
        "preset_factors": dict(PRESET_FACTORS),
        "crf_min": CRF_MIN,
        "crf_max": CRF_MAX,
        "audio_choices": list(AUDIO_CHOICES),
        "fps_choices": list(FPS_CHOICES),
        "min_width": MIN_WIDTH,
        "max_width": MAX_WIDTH,
        "anchor_bpp": ANCHOR_BPP,
        "anchor_crf": ANCHOR_CRF,
        "crf_halving": CRF_HALVING,
        "container_overhead": CONTAINER_OVERHEAD,
    }


__all__ = [
    "AUDIO_CHOICES",
    "command_settings",
    "CRF_MAX",
    "CRF_MIN",
    "DEFAULTS",
    "FPS_CHOICES",
    "MAX_WIDTH",
    "MIN_WIDTH",
    "PRESETS",
    "PRESET_FACTORS",
    "estimate_bytes",
    "is_default",
    "model",
    "normalize",
    "scale_filter",
]
