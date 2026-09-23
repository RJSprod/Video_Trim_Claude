/* Video Trim — the browser player.
 *
 * A port of videotrim/player.py's A-B loop semantics onto an HTML5 <video>, so
 * the rules are the same ones the desktop window follows:
 *
 *   - tap 1 sets A, tap 2 sets B, tap 3 clears both;
 *   - once both exist every seek is confined to [A, B];
 *   - Stop returns to A, Repeat wraps B back to A;
 *   - scrubbing previews live and restores the play state on release.
 *
 * Where the work happens depends on where the file is. A file you pick on this
 * device plays from a blob URL and is never sent anywhere; its stills are
 * captured here on a canvas, and only the PNG travels. Cutting a clip is the
 * one thing the browser cannot do at full quality, so that — and only that —
 * sends the source to the host, where ffmpeg cuts from the original.
 *
 * Saved files are created in whatever folder the host chose. This page never
 * learns that folder's path unless the server decided this session may see one.
 */
(function () {
  "use strict";

  var SKIP_MS = 5000;
  var MIN_LOOP_MS = 120;          // player.py: MIN_LOOP_MS
  var SEEK_GUARD_MS = 260;        // player.py: SEEK_GUARD_MS
  var SCRUB_THROTTLE_MS = 60;     // player.py: SCRUB_THROTTLE_MS
  var BOUNDS_TICK_MS = 25;
  var FALLBACK_FPS = 25;
  var AUTOHIDE_MS = 3000;
  var DOUBLE_TAP_MS = 260;
  var JOB_POLL_MS = 400;

  var dom = {};
  var config = { destination: "the host's save folder", ffmpeg: true };

  /* Drawn rather than typed. The desktop app builds its glyphs as vectors in
   * icons.py for the same reason: font and emoji coverage varies per platform,
   * and a control bar of mismatched emoji looks broken. */
  function svg(body, stroke) {
    return '<svg viewBox="0 0 20 20" aria-hidden="true" focusable="false" ' +
      (stroke
        ? 'fill="none" stroke="currentColor" stroke-width="1.7" ' +
          'stroke-linecap="round" stroke-linejoin="round">'
        : 'fill="currentColor">') + body + "</svg>";
  }

  var ICONS = {
    play: svg('<path d="M6.5 3.6v12.8L17 10z"/>'),
    pause: svg('<path d="M5.6 3.8h3.1v12.4H5.6zM11.3 3.8h3.1v12.4h-3.1z"/>'),
    stop: svg('<rect x="4.6" y="4.6" width="10.8" height="10.8" rx="1.4"/>'),
    prevFrame: svg('<path d="M4 4.2h2.1v11.6H4z"/><path d="M16.4 4.4L7.6 10l8.8 5.6z"/>'),
    nextFrame: svg('<path d="M13.9 4.2H16v11.6h-2.1z"/><path d="M3.6 4.4L12.4 10 3.6 15.6z"/>'),
    back5: svg('<path d="M11 5.5L6.5 10l4.5 4.5M16 5.5L11.5 10l4.5 4.5"/>', true) + "<b>5</b>",
    fwd5: "<b>5</b>" + svg('<path d="M9 5.5l4.5 4.5L9 14.5M4 5.5L8.5 10 4 14.5"/>', true),
    repeat: svg('<path d="M4.2 8.6a6 6 0 0 1 10-2.6l1.6 1.6M15.8 11.4a6 6 0 0 1-10 2.6L4.2 12.4"/>' +
      '<path d="M16.4 3.6v4h-4M3.6 16.4v-4h4"/>', true),
    /* The two A-B saves share a download arrow at the lower right, so they read
     * as a pair; what sits beside it says which one: a frame, or a note. */
    saveVideo: svg('<rect x="2.2" y="3.4" width="11.2" height="9.2" rx="1.6"/>' +
      '<path d="M6.6 5.9v4.2L10 8z"/>' +
      '<path d="M16 9.4v7.2M13.6 14.2l2.4 2.4 2.4-2.4"/>', true),
    saveAudio: svg('<path d="M5.8 13V5.1l5.6-1.5v7.9"/>' +
      '<circle cx="4.1" cy="13" r="1.8"/><circle cx="9.7" cy="11.5" r="1.8"/>' +
      '<path d="M16 9.4v7.2M13.6 14.2l2.4 2.4 2.4-2.4"/>', true),
    camera: svg('<path d="M3.2 7.4A1.6 1.6 0 0 1 4.8 5.8h1.6l1-1.6h5.2l1 1.6h1.6a1.6 1.6 0 0 1 1.6 1.6v6.4a1.6 1.6 0 0 1-1.6 1.6H4.8a1.6 1.6 0 0 1-1.6-1.6z"/>' +
      '<circle cx="10" cy="10.6" r="2.8"/>', true),
    volumeOn: svg('<path d="M4 7.6h2.4L9.8 4.8v10.4L6.4 12.4H4z"/>' +
      '<path d="M12.6 7.4a3.4 3.4 0 0 1 0 5.2M14.8 5.6a6 6 0 0 1 0 8.8"/>', true),
    volumeOff: svg('<path d="M4 7.6h2.4L9.8 4.8v10.4L6.4 12.4H4z"/>' +
      '<path d="M12.8 8.2l4 3.6M16.8 8.2l-4 3.6"/>', true),
    fullscreen: svg('<path d="M4 7.6V4h3.6M16 12.4V16h-3.6M12.4 4H16v3.6M7.6 16H4v-3.6"/>', true),
    folder: svg('<path d="M3.4 6.4a1.4 1.4 0 0 1 1.4-1.4h2.8l1.4 2h5.8a1.4 1.4 0 0 1 1.4 1.4v6a1.4 1.4 0 0 1-1.4 1.4H4.8a1.4 1.4 0 0 1-1.4-1.4z"/>', true),
    up: svg('<path d="M10 15.6V4.8M5.6 9.2L10 4.8l4.4 4.4"/>', true),
    file: svg('<path d="M7.4 6.6L14 10l-6.6 3.4z"/>'),
    gear: svg('<circle cx="10" cy="10" r="2.6"/>' +
      '<path d="M10 2.9l1 2.1 2.3-.5 1.2 1.2-.5 2.3 2.1 1v1.7l-2.1 1 .5 2.3-1.2 1.2-2.3-.5-1 2.1H8.9l-1-2.1-2.3.5-1.2-1.2.5-2.3-2.1-1V9l2.1-1-.5-2.3 1.2-1.2 2.3.5 1-2.1z"/>', true),
    upload: svg('<path d="M10 13.4V3.6M6.6 7L10 3.6 13.4 7M4.2 13.6v2.2a1 1 0 0 0 1 1h9.6a1 1 0 0 0 1-1v-2.2"/>', true)
  };

  var st = {
    mounted: false,
    local: true,          // viewing from the machine running the server?
    media: null,          // the source: what exports are cut from
    playbackUrl: "",      // what the <video> is actually playing (may be a proxy)
    a: null,
    b: null,
    repeat: false,
    pinned: false,
    scrubbing: false,
    scrubResume: false,
    scrubPending: null,
    scrubTimer: 0,
    scrubLast: 0,
    guardUntil: 0,
    frameStartMs: -1,
    boundsTimer: 0,
    docWired: false,
    hideTimer: 0,
    toastTimer: 0,
    tapTimer: 0,
    flashTimers: {},
    busy: null,           // an in-flight clip/proxy job id
    saved: [],
    browseDir: "",
    localFile: null,      // a File being played from this device, never sent
    objectUrl: "",        // its blob URL, revoked when another file replaces it
    fpsSamples: [],       // frame gaps, for measuring the rate of a local file
    options: null,        // what the gear menu chose, or null for "as shipped"
    optionsOpen: false
  };

  /* The export model — bitrate curve, choices, defaults — comes from the
   * server's videotrim/encoding.py so the estimate under the slider and the
   * encode that eventually runs are described by the same numbers. These are
   * only a stand-in until /vt/api/config answers. */
  var MODEL = {
    defaults: { width: 0, crf: 18, preset: "veryfast", fps_cap: 0, audio_kbps: 192 },
    presets: ["ultrafast", "superfast", "veryfast", "faster", "fast", "medium",
              "slow", "slower"],
    preset_factors: { ultrafast: 1.65, superfast: 1.35, veryfast: 1.2, faster: 1.1,
                      fast: 1.05, medium: 1, slow: 0.93, slower: 0.88 },
    crf_min: 14, crf_max: 34,
    audio_choices: [0, 96, 128, 192, 256],
    fps_choices: [0, 15, 24, 30, 60],
    min_width: 128, max_width: 7680,
    anchor_bpp: 0.0643, anchor_crf: 23, crf_halving: 6, container_overhead: 0.005
  };

  var OPTIONS_KEY = "vt.export.options";
  var SCALE_STEPS = [100, 75, 50, 33, 25];

  // --- formatting (mirrors videotrim/timefmt.py) -----------------------------
  function fmtTime(ms, tenths) {
    ms = Math.max(0, Math.floor(ms || 0));
    var rem = ms % 1000;
    var secs = Math.floor(ms / 1000);
    var mins = Math.floor(secs / 60);
    secs = secs % 60;
    var hours = Math.floor(mins / 60);
    mins = mins % 60;
    var pad = function (n) { return n < 10 ? "0" + n : "" + n; };
    var base = hours ? hours + ":" + pad(mins) + ":" + pad(secs) : mins + ":" + pad(secs);
    return tenths ? base + "." + Math.floor(rem / 100) : base;
  }

  function fmtSize(bytes) {
    if (!bytes) return "";
    var units = ["B", "KB", "MB", "GB", "TB"];
    var index = 0;
    var value = bytes;
    while (value >= 1024 && index < units.length - 1) { value /= 1024; index += 1; }
    return (value >= 10 || index === 0 ? Math.round(value) : value.toFixed(1)) + " " + units[index];
  }

  /* Paint an icon, skipping the DOM write when it is already the right one.
   * Safe only after paintIcons() has filled the initially-empty nodes. */
  function setIcon(node, name) {
    if (!node || node.getAttribute("data-icon") === name) return;
    node.setAttribute("data-icon", name);
    node.innerHTML = ICONS[name] || "";
  }

  /* Fill every node the markup declared an icon for. The server ships empty
   * buttons carrying data-icon so the icon set lives in exactly one place. */
  function paintIcons(root) {
    Array.prototype.forEach.call(root.querySelectorAll("[data-icon]"), function (node) {
      var name = node.getAttribute("data-icon");
      if (ICONS[name]) node.innerHTML = ICONS[name];
    });
  }

  // --- state helpers ---------------------------------------------------------
  function hasMedia() { return !!st.media; }

  function hasLoop() { return st.a !== null && st.b !== null; }

  function durationMs() {
    if (st.media && st.media.duration_ms > 0) return st.media.duration_ms;
    var video = dom.video;
    if (video && isFinite(video.duration) && video.duration > 0) {
      return Math.round(video.duration * 1000);
    }
    return 0;
  }

  function positionMs() {
    return dom.video ? dom.video.currentTime * 1000 : 0;
  }

  /* The window playback is allowed to move within — player.py: bounds(). */
  function bounds() {
    if (hasLoop()) return [st.a, st.b];
    return [0, durationMs()];
  }

  function frameMs() {
    var fps = st.media && st.media.fps > 0 ? st.media.fps : FALLBACK_FPS;
    return 1000 / fps;
  }

  function isPlaying() {
    return dom.video && !dom.video.paused && !dom.video.ended;
  }

  // --- seeking ---------------------------------------------------------------
  function rawSeek(ms) {
    if (!dom.video) return;
    var limit = durationMs();
    var target = Math.max(0, ms);
    if (limit > 0) target = Math.min(target, limit);
    st.guardUntil = performance.now() + SEEK_GUARD_MS;
    dom.video.currentTime = target / 1000;
    render();
  }

  /* Seek from the UI, clamped into the A-B range — player.py: seek(). */
  function seek(ms) {
    var range = bounds();
    rawSeek(Math.max(range[0], Math.min(ms, Math.max(range[0], range[1] - 20))));
  }

  function play() {
    if (!hasMedia()) return;
    var range = bounds();
    var pos = positionMs();
    var atEnd = dom.video.ended;
    // Pressing play from outside the range snaps to A first.
    if (atEnd || pos < range[0] || pos >= Math.max(range[0], range[1] - 40)) {
      rawSeek(range[0]);
    }
    var promise = dom.video.play();
    if (promise && promise.catch) {
      promise.catch(function () { /* autoplay refusal is harmless here */ });
    }
  }

  function pause() {
    if (dom.video) dom.video.pause();
  }

  function toggle() {
    if (isPlaying()) pause(); else play();
  }

  /* Pause and return to the start of the active range (A, or 0). */
  function stop() {
    if (!hasMedia()) return;
    pause();
    rawSeek(bounds()[0]);
  }

  function skip(delta) {
    if (!hasMedia()) return;
    var range = bounds();
    var target = positionMs() + delta;
    target = Math.max(range[0], Math.min(target, Math.max(range[0], range[1] - 60)));
    rawSeek(target);
  }

  /* Move exactly one frame and pause — player.py: step_frames().
   *
   * Anchored on the displayed frame's own presentation time (mediaTime from
   * requestVideoFrameCallback) rather than on currentTime, so repeated steps
   * cannot drift and stepping back lands on precisely the frame you left.
   *
   * The target is then nudged a quarter of a frame *forward* — always forward,
   * whichever way we are stepping, because that lands inside the neighbouring
   * frame in both directions. Some margin is needed because the measured frame
   * rate can be a rounded 29.97 → 30, which would leave a landing exactly on a
   * boundary ambiguous; a quarter absorbs that while keeping the readout on the
   * frame's own tenth of a second. */
  function stepFrames(direction) {
    if (!hasMedia()) return;
    pause();
    var frame = frameMs();
    var target;
    if (st.frameStartMs >= 0) {
      target = st.frameStartMs + direction * frame + frame / 4;
    } else {
      target = positionMs() + direction * frame;
    }
    var range = bounds();
    rawSeek(Math.max(range[0], Math.min(target, Math.max(range[0], range[1] - 1))));
  }

  // --- A-B markers -----------------------------------------------------------
  function cycleMarker() {
    if (!hasMedia()) return;
    var pos = positionMs();
    var message;
    if (st.a === null) {
      st.a = pos;
      message = "Loop point A set";
    } else if (st.b === null) {
      if (Math.abs(pos - st.a) < MIN_LOOP_MS) {
        toast("Move further along before setting B");
        return;
      }
      // Marking "backwards" reorders rather than failing.
      var low = Math.min(st.a, pos);
      var high = Math.max(st.a, pos);
      st.a = low;
      st.b = high;
      message = "A-B loop set";
    } else {
      st.a = null;
      st.b = null;
      message = "A-B loop cleared";
    }

    if (hasLoop()) {
      var range = bounds();
      var now = positionMs();
      if (!(now >= range[0] && now < range[1])) rawSeek(range[0]);
    }
    render();
    toast(message);
  }

  function clearMarkers() {
    st.a = null;
    st.b = null;
    render();
  }

  /* Keep playback inside [A, B] — player.py: _enforce_bounds(). */
  function enforceBounds() {
    if (!hasLoop() || !isPlaying()) return;
    if (performance.now() < st.guardUntil || dom.video.seeking) return;
    var range = bounds();
    var pos = positionMs();
    if (pos >= range[1] - 30) {
      if (st.repeat) {
        rawSeek(range[0]);
      } else {
        pause();
        rawSeek(Math.max(range[0], range[1] - 40));
      }
    } else if (pos < range[0] - 250) {
      rawSeek(range[0]);
    }
  }

  function stopBoundsWatch() {
    window.clearInterval(st.boundsTimer);
    st.boundsTimer = 0;
  }

  function onEnded() {
    var range = bounds();
    if (st.repeat) {
      rawSeek(range[0]);
      play();
    } else {
      pause();
      rawSeek(range[0]);
    }
  }

  // --- scrubbing -------------------------------------------------------------
  function scrubPosition(event) {
    var rect = dom.scrub.getBoundingClientRect();
    if (!rect.width) return 0;
    var ratio = (event.clientX - rect.left) / rect.width;
    ratio = Math.max(0, Math.min(1, ratio));
    var total = durationMs();
    var range = bounds();
    // Dragging maps across the whole timeline, then clamps into the A-B range,
    // matching how the desktop scrubber behaves.
    return Math.max(range[0], Math.min(ratio * total, Math.max(range[0], range[1] - 20)));
  }

  function flushScrub() {
    if (st.scrubPending === null) return;
    var target = st.scrubPending;
    st.scrubPending = null;
    rawSeek(target);
    st.scrubTimer = window.setTimeout(function () {
      st.scrubTimer = 0;
      flushScrub();
    }, SCRUB_THROTTLE_MS);
  }

  function beginScrub(event) {
    if (!hasMedia()) return;
    st.scrubbing = true;
    st.scrubResume = isPlaying();
    if (st.scrubResume) pause();
    dom.scrub.classList.add("vt-scrubbing");
    try { dom.scrub.setPointerCapture(event.pointerId); } catch (err) { /* ignore */ }
    rawSeek(scrubPosition(event));
    bumpAutoHide();
  }

  function moveScrub(event) {
    if (!st.scrubbing) return;
    st.scrubPending = scrubPosition(event);
    if (!st.scrubTimer) flushScrub();
  }

  function endScrub(event) {
    if (!st.scrubbing) return;
    st.scrubbing = false;
    dom.scrub.classList.remove("vt-scrubbing");
    if (st.scrubTimer) { window.clearTimeout(st.scrubTimer); st.scrubTimer = 0; }
    st.scrubPending = null;
    // The exact release position always wins over the throttled previews.
    rawSeek(scrubPosition(event));
    if (st.scrubResume) {
      st.scrubResume = false;
      var promise = dom.video.play();
      if (promise && promise.catch) promise.catch(function () {});
    }
    bumpAutoHide();
  }

  // --- rendering -------------------------------------------------------------
  function render() {
    if (!st.mounted) return;
    var total = durationMs();
    var pos = positionMs();

    dom.pos.textContent = fmtTime(pos, true);
    dom.dur.textContent = fmtTime(total);

    var pct = total > 0 ? Math.max(0, Math.min(100, (pos / total) * 100)) : 0;
    dom.played.style.width = pct + "%";
    dom.handle.style.left = pct + "%";
    dom.scrub.setAttribute("aria-valuemax", String(Math.round(total)));
    dom.scrub.setAttribute("aria-valuenow", String(Math.round(pos)));
    dom.scrub.setAttribute("aria-valuetext", fmtTime(pos, true));

    var buffered = 0;
    if (dom.video.buffered && dom.video.buffered.length) {
      buffered = dom.video.buffered.end(dom.video.buffered.length - 1) * 1000;
    }
    dom.buffer.style.width = (total > 0 ? Math.min(100, (buffered / total) * 100) : 0) + "%";

    var aPct = st.a !== null && total > 0 ? (st.a / total) * 100 : 0;
    var bPct = st.b !== null && total > 0 ? (st.b / total) * 100 : 0;
    dom.markA.style.display = st.a !== null ? "block" : "none";
    dom.markA.style.left = aPct + "%";
    dom.markB.style.display = st.b !== null ? "block" : "none";
    dom.markB.style.left = bPct + "%";
    dom.abFill.style.display = hasLoop() ? "block" : "none";
    if (hasLoop()) {
      dom.abFill.style.left = aPct + "%";
      dom.abFill.style.width = Math.max(0, bPct - aPct) + "%";
    }

    if (hasLoop()) {
      dom.timeAb.textContent = "A-B  " + fmtTime(st.a, true) + " → " + fmtTime(st.b, true) +
        "   (" + fmtTime(st.b - st.a, true) + ")";
      dom.timeAb.setAttribute("data-set", "1");
    } else if (st.a !== null) {
      dom.timeAb.textContent = "A at " + fmtTime(st.a, true) + " — set B to close the loop";
      dom.timeAb.setAttribute("data-set", "1");
    } else {
      dom.timeAb.textContent = "no A-B range";
      dom.timeAb.removeAttribute("data-set");
    }

    var playing = isPlaying();
    setIcon(dom.btn.play, playing ? "pause" : "play");
    dom.btn.play.title = playing ? "Pause (Space)" : "Play (Space)";
    dom.btn.repeat.setAttribute("data-on", st.repeat ? "1" : "0");
    dom.btn.mute.setAttribute("data-on", dom.video.muted ? "1" : "0");
    dom.btn.mute.title = dom.video.muted ? "Unmute (M)" : "Mute (M)";
    setIcon(dom.btn.mute, dom.video.muted ? "volumeOff" : "volumeOn");
    dom.btn.marker.setAttribute("data-stage", hasLoop() ? "2" : (st.a !== null ? "1" : "0"));
    dom.btn.clip.disabled = !hasLoop() || !!st.busy || !config.ffmpeg;
    dom.btn.audio.disabled = dom.btn.clip.disabled;
    dom.btn.screenshot.disabled = !hasMedia() || !config.ffmpeg;

    ["stop", "back5", "fwd5", "prevFrame", "nextFrame", "marker", "repeat"].forEach(function (key) {
      if (dom.btn[key]) dom.btn[key].disabled = !hasMedia();
    });

    if (dom.btn.options) dom.btn.options.disabled = !config.ffmpeg;
    if (st.optionsOpen) renderOptions();
  }

  // --- toasts ----------------------------------------------------------------
  function toast(message, ms, options) {
    if (!st.mounted) return;
    options = options || {};
    window.clearTimeout(st.toastTimer);
    dom.toastText.textContent = message;
    dom.toast.setAttribute("data-tone", options.tone || "info");

    dom.toastAction.hidden = !options.action;
    if (options.action) {
      dom.toastAction.textContent = options.action.label;
      dom.toastAction.onclick = function () {
        hideToast();
        options.action.run();
      };
    } else {
      dom.toastAction.onclick = null;
    }

    dom.toast.setAttribute("data-on", "1");
    var life = ms === undefined ? 2200 : ms;
    if (life > 0) {
      st.toastTimer = window.setTimeout(hideToast, life);
    }
  }

  function hideToast() {
    window.clearTimeout(st.toastTimer);
    dom.toast.setAttribute("data-on", "0");
    dom.toastAction.hidden = true;
  }

  function fail(message) {
    toast(message, 5200, { tone: "error" });
  }

  // --- control visibility ----------------------------------------------------
  function showPanel() {
    dom.panel.setAttribute("data-hidden", "0");
    dom.stage.classList.remove("vt-hide-cursor");
  }

  function hidePanel() {
    if (st.pinned || !isPlaying()) return;
    dom.panel.setAttribute("data-hidden", "1");
    dom.stage.classList.add("vt-hide-cursor");
  }

  /* Reveal the bar and restart the fade timer. Pinned bars and paused playback
   * never fade — only a reveal the user did not ask for does. */
  function bumpAutoHide() {
    showPanel();
    window.clearTimeout(st.hideTimer);
    if (st.pinned || !isPlaying()) return;
    st.hideTimer = window.setTimeout(hidePanel, AUTOHIDE_MS);
  }

  function pinControls(pinned) {
    st.pinned = pinned;
    if (pinned) {
      window.clearTimeout(st.hideTimer);
      showPanel();
    } else {
      bumpAutoHide();
      if (isPlaying()) hidePanel();
    }
  }

  function flash(side, label) {
    var node = side === "left" ? dom.flashLeft : dom.flashRight;
    node.querySelector("span").textContent = label;
    node.setAttribute("data-on", "1");
    window.clearTimeout(st.flashTimers[side]);
    st.flashTimers[side] = window.setTimeout(function () {
      node.setAttribute("data-on", "0");
    }, 480);
  }

  // --- server calls ----------------------------------------------------------
  /* Read the CSRF companion cookie. It is deliberately readable: the value is
   * echoed in a custom header, which a cross-site form cannot set, and it is
   * checked against the session's own token server-side. */
  function csrfToken() {
    var parts = (document.cookie || "").split(";");
    for (var i = 0; i < parts.length; i++) {
      var pair = parts[i].trim();
      var eq = pair.indexOf("=");
      if (eq > 0 && pair.slice(0, eq) === "vt_csrf") return pair.slice(eq + 1);
    }
    return "";
  }

  function request(url, options) {
    options = options || {};
    options.credentials = "same-origin";
    options.headers = options.headers || {};
    var method = (options.method || "GET").toUpperCase();
    if (method !== "GET" && method !== "HEAD") {
      options.headers["X-VT-CSRF"] = csrfToken();
    }
    return fetch(url, options).then(function (response) {
      if (response.status === 401) {
        window.location.href = "/login";
        throw new Error("Login required");
      }
      var isJson = (response.headers.get("content-type") || "").indexOf("json") >= 0;
      return (isJson ? response.json() : response.text()).then(function (body) {
        if (!response.ok) {
          var detail = body && body.detail ? body.detail : ("Request failed (" + response.status + ")");
          throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
        }
        return body;
      });
    });
  }

  function postJson(url, payload) {
    return request(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload || {})
    });
  }

  /* Present whichever way of opening a file actually works from here.
   *
   * Reading paths on the host is a separate capability from being the host, and
   * is refused for anyone who does not have it. Rather than let a visitor type a
   * path and collect a 403, the path row and the folder browser are taken away
   * and upload becomes the way in. */
  function applyReach() {
    var local = st.local;

    var row = document.getElementById("vt-source-row");
    if (row) row.style.display = local ? "" : "none";

    var localHint = document.getElementById("vt-placeholder-local");
    if (localHint) localHint.hidden = !local;

    var title = document.getElementById("vt-placeholder-title");
    if (title) {
      title.textContent = local
        ? "Drop a video here"
        : "Drop a video here to send it to " + hostLabel();
    }

    // The folder button opens the host browser at the host, and the upload
    // picker everywhere else — the same "get me a video" slot either way.
    if (dom.btn.browse) {
      setIcon(dom.btn.browse, local ? "folder" : "upload");
      dom.btn.browse.title = local ? "Open a video (O)" : "Upload a video (O)";
    }

    /* Whatever the server called the destination is what gets shown. It is a
     * real path only when the server decided this session may see one; every
     * other session gets a display name and never a filesystem path. */
    if (dom.outputNote) {
      if (config.output_configured === false) {
        dom.outputNote.textContent =
          "Save location is unavailable — the host has not chosen one yet, so " +
          "clips and stills cannot be saved.";
      } else if (config.can_write === false) {
        dom.outputNote.textContent = config.write_reason || "File transfer disabled by host";
      } else if (local) {
        dom.outputNote.textContent =
          "Clips and stills are created in " + config.destination + " on this machine.";
      } else {
        // Say plainly what leaves the device, because that is the question
        // somebody picking a 4 GB file actually has.
        dom.outputNote.textContent =
          "A video you choose here plays on this device and is not sent anywhere. " +
          "Stills are captured here and only the picture is sent. Cutting a clip " +
          "needs the video on " + hostLabel() + ", so that is when it is sent. " +
          "Saved files are created in " + config.destination +
          " — nothing already there is replaced.";
      }
    }
  }

  function hostLabel() {
    return window.location.hostname || "the host";
  }

  // --- export options --------------------------------------------------------
  /* The gear menu. It lives inside the control bar, so it is on screen exactly
   * when the controls are and cannot be left open over a bare video.
   *
   * Everything here is a *request*. The server re-derives the frame size from
   * the source it probed and clamps every number to the same bounds again, so
   * nothing in this file is what keeps an export sane — it is what makes the
   * choice visible, and what puts a size next to it before you commit.
   */
  function defaultOptions() {
    var base = MODEL.defaults || {};
    return {
      width: 0,
      crf: base.crf,
      preset: base.preset,
      fps_cap: base.fps_cap,
      audio_kbps: base.audio_kbps
    };
  }

  function options() {
    if (!st.options) st.options = defaultOptions();
    return st.options;
  }

  function loadStoredOptions() {
    try {
      var raw = window.localStorage.getItem(OPTIONS_KEY);
      if (!raw) return;
      var saved = JSON.parse(raw);
      if (!saved || typeof saved !== "object") return;
      var current = defaultOptions();
      // Scale is remembered as a percentage: a width from one video means
      // nothing for the next one, but "half size" always does.
      if (typeof saved.scale_percent === "number") {
        current.scale_percent = clampNumber(saved.scale_percent, 10, 100);
      }
      ["crf", "fps_cap", "audio_kbps"].forEach(function (key) {
        if (typeof saved[key] === "number") current[key] = saved[key];
      });
      if (MODEL.presets.indexOf(saved.preset) >= 0) current.preset = saved.preset;
      current.crf = clampNumber(current.crf, MODEL.crf_min, MODEL.crf_max);
      if (MODEL.audio_choices.indexOf(current.audio_kbps) < 0) {
        current.audio_kbps = MODEL.defaults.audio_kbps;
      }
      if (MODEL.fps_choices.indexOf(current.fps_cap) < 0) current.fps_cap = 0;
      st.options = current;
    } catch (err) { /* a corrupt preference is not worth a broken player */ }
  }

  function storeOptions() {
    try {
      var current = options();
      window.localStorage.setItem(OPTIONS_KEY, JSON.stringify({
        scale_percent: current.scale_percent || 100,
        crf: current.crf,
        preset: current.preset,
        fps_cap: current.fps_cap,
        audio_kbps: current.audio_kbps
      }));
    } catch (err) { /* private browsing; the options still work for this session */ }
  }

  function clampNumber(value, low, high) {
    value = Number(value);
    if (!isFinite(value)) return low;
    return Math.max(low, Math.min(high, value));
  }

  function evenNumber(value, floor) {
    var number = Math.round(Number(value) || 0);
    if (number % 2) number -= 1;
    return Math.max(floor || 2, number);
  }

  /* The source's own pixel size. A file on the host was probed; one playing
   * from this device reports it once the browser has the metadata. */
  function sourceSize() {
    var width = (st.media && st.media.width) || (dom.video && dom.video.videoWidth) || 0;
    var height = (st.media && st.media.height) || (dom.video && dom.video.videoHeight) || 0;
    return { width: Math.round(width), height: Math.round(height) };
  }

  function sourceFps() {
    var fps = (st.media && st.media.fps) || 0;
    return fps > 0 ? fps : FALLBACK_FPS;
  }

  /* The width an export would come out at, in pixels. Kept as a percentage in
   * state and turned into pixels here, against whatever is loaded now. */
  function targetWidth() {
    var size = sourceSize();
    var percent = options().scale_percent || 100;
    if (!size.width || percent >= 100) return size.width;
    return evenNumber(size.width * percent / 100, MODEL.min_width);
  }

  function targetHeight() {
    var size = sourceSize();
    if (!size.width || !size.height) return 0;
    var width = targetWidth();
    if (width >= size.width) return size.height;
    return evenNumber(width * size.height / size.width, 2);
  }

  function optionsPayload() {
    /* What the clip route is sent. Only ``width`` travels — the height is the
     * server's to derive from the source it probed, which is what keeps the
     * aspect ratio locked no matter what this page believes. */
    var current = options();
    var size = sourceSize();
    var width = targetWidth();
    return {
      width: (size.width && width && width < size.width) ? width : 0,
      crf: current.crf,
      preset: current.preset,
      fps_cap: current.fps_cap,
      audio_kbps: current.audio_kbps
    };
  }

  function optionsAreDefault() {
    var current = options();
    var base = MODEL.defaults;
    return (current.scale_percent || 100) === 100
      && current.crf === base.crf
      && current.preset === base.preset
      && current.fps_cap === base.fps_cap
      && current.audio_kbps === base.audio_kbps;
  }

  /* The same curve as videotrim/encoding.py: bits per pixel per frame, halved
   * every crf_halving steps, times a factor for how hard the preset looks. */
  function estimateBytes(durationMs) {
    var seconds = Math.max(0, durationMs || 0) / 1000;
    if (seconds <= 0) return 0;
    var width = targetWidth();
    var height = targetHeight();
    if (!width || !height) return 0;

    var current = options();
    var rate = sourceFps();
    if (current.fps_cap) rate = Math.min(rate, current.fps_cap);

    var bpp = MODEL.anchor_bpp
      * Math.pow(2, (MODEL.anchor_crf - current.crf) / MODEL.crf_halving)
      * (MODEL.preset_factors[current.preset] || 1);
    var bits = bpp * width * height * rate + current.audio_kbps * 1000;
    return Math.round(bits * seconds / 8 * (1 + MODEL.container_overhead));
  }

  function crfWords(crf) {
    if (crf <= 16) return "near-lossless";
    if (crf <= 20) return "high quality";
    if (crf <= 24) return "good";
    if (crf <= 28) return "compressed";
    return "small and soft";
  }

  /* Choice lists are repainted whenever the model changes; the listeners on the
   * static controls are attached once per set of nodes. The flag lives on the
   * node itself, so a re-mount against fresh DOM wires those fresh nodes. */
  function buildOptionsUI() {
    if (!dom.options) return;
    paintOptionChoices();
    if (dom.options.getAttribute("data-wired") !== "1") {
      wireOptionControls();
      dom.options.setAttribute("data-wired", "1");
    }
    renderOptions();
  }

  function paintOptionChoices() {
    dom.optScales.innerHTML = "";
    SCALE_STEPS.forEach(function (percent) {
      var button = document.createElement("button");
      button.type = "button";
      button.className = "vt-scale";
      button.textContent = percent + "%";
      button.setAttribute("data-percent", String(percent));
      button.addEventListener("click", function () {
        options().scale_percent = percent;
        storeOptions();
        renderOptions();
      });
      dom.optScales.appendChild(button);
    });

    dom.optCrf.min = String(MODEL.crf_min);
    dom.optCrf.max = String(MODEL.crf_max);
    dom.optCrf.step = "1";

    dom.optPreset.innerHTML = "";
    MODEL.presets.forEach(function (name) {
      var option = document.createElement("option");
      option.value = name;
      option.textContent = name;
      dom.optPreset.appendChild(option);
    });

    dom.optFps.innerHTML = "";
    MODEL.fps_choices.forEach(function (value) {
      var option = document.createElement("option");
      option.value = String(value);
      option.textContent = value ? "Cap at " + value + " fps" : "Same as source";
      dom.optFps.appendChild(option);
    });

    dom.optAudio.innerHTML = "";
    MODEL.audio_choices.forEach(function (value) {
      var option = document.createElement("option");
      option.value = String(value);
      option.textContent = value ? value + " kbps AAC" : "No audio";
      dom.optAudio.appendChild(option);
    });
  }

  function wireOptionControls() {
    // The slider reads left-to-right as "bigger file → smaller file", which is
    // the opposite of CRF's own direction, so the value is mirrored.
    dom.optCrf.addEventListener("input", function () {
      options().crf = MODEL.crf_min + MODEL.crf_max - Number(dom.optCrf.value);
      renderOptions();
    });
    dom.optCrf.addEventListener("change", storeOptions);

    dom.optPreset.addEventListener("change", function () {
      options().preset = dom.optPreset.value;
      storeOptions();
      renderOptions();
    });

    dom.optFps.addEventListener("change", function () {
      options().fps_cap = Number(dom.optFps.value);
      storeOptions();
      renderOptions();
    });

    dom.optAudio.addEventListener("change", function () {
      options().audio_kbps = Number(dom.optAudio.value);
      storeOptions();
      renderOptions();
    });

    /* Typing a width picks the nearest percentage of the source rather than
     * storing pixels: the aspect ratio then stays locked when the next video
     * loads at a different size, and the server derives the height either way. */
    dom.optWidth.addEventListener("change", function () {
      var size = sourceSize();
      var wanted = Number(dom.optWidth.value);
      if (!size.width || !isFinite(wanted) || wanted <= 0) {
        renderOptions();
        return;
      }
      wanted = clampNumber(wanted, MODEL.min_width, size.width);
      options().scale_percent = clampNumber(
        Math.round(wanted / size.width * 100), 10, 100);
      storeOptions();
      renderOptions();
    });
  }

  function renderOptions() {
    if (!dom.options) return;
    var current = options();
    var size = sourceSize();
    var width = targetWidth();
    var height = targetHeight();
    var percent = current.scale_percent || 100;

    dom.optSource.textContent = size.width && size.height
      ? "source " + size.width + "×" + size.height
      : "source size unknown";

    Array.prototype.forEach.call(dom.optScales.children, function (button) {
      var value = Number(button.getAttribute("data-percent"));
      button.setAttribute("data-on", value === percent ? "1" : "0");
      button.disabled = !size.width;
      if (size.width) {
        button.title = evenNumber(size.width * value / 100, MODEL.min_width) + " px wide";
      }
    });

    dom.optWidth.min = String(MODEL.min_width);
    dom.optWidth.max = String(size.width || MODEL.max_width);
    dom.optWidth.value = width ? String(width) : "";
    dom.optWidth.disabled = !size.width;
    dom.optHeight.value = height ? String(height) : "";

    dom.optCrf.value = String(MODEL.crf_min + MODEL.crf_max - current.crf);
    dom.optCrfLabel.textContent = "CRF " + current.crf + " — " + crfWords(current.crf);
    dom.optPreset.value = current.preset;
    dom.optFps.value = String(current.fps_cap);
    dom.optAudio.value = String(current.audio_kbps);

    var total = durationMs();
    var full = estimateBytes(total);
    dom.estFull.textContent = full ? "≈ " + fmtSize(full) : "—";
    if (hasLoop()) {
      dom.estRangeLabel.textContent = "A-B range  " + fmtTime(st.b - st.a);
      dom.estRange.textContent = "≈ " + fmtSize(estimateBytes(st.b - st.a));
    } else {
      dom.estRangeLabel.textContent = "A-B range";
      dom.estRange.textContent = "not set";
    }
    dom.estNote.textContent = total
      ? "The whole-video figure is the largest this can get: A at the start, B at " +
        "the end. Estimates, not promises — busy footage encodes larger."
      : "Open a video to see how large an export would be.";

    if (dom.btn.options) {
      dom.btn.options.setAttribute("data-on", optionsAreDefault() ? "0" : "1");
      dom.btn.options.setAttribute("aria-expanded", st.optionsOpen ? "true" : "false");
    }
  }

  function toggleOptions(open) {
    if (!dom.options) return;
    st.optionsOpen = open === undefined ? !st.optionsOpen : !!open;
    dom.options.hidden = !st.optionsOpen;
    // An open menu must not have the bar fade out from under it.
    if (st.optionsOpen) {
      buildOptionsUI();
      renderOptions();
      window.clearTimeout(st.hideTimer);
      showPanel();
    } else {
      bumpAutoHide();
    }
    if (dom.btn.options) {
      dom.btn.options.setAttribute("aria-expanded", st.optionsOpen ? "true" : "false");
    }
  }

  function resetOptions() {
    st.options = defaultOptions();
    storeOptions();
    renderOptions();
    toast("Export options back to defaults.", 1800);
  }

  // --- loading media ---------------------------------------------------------
  function loadMedia(info) {
    st.media = info;
    st.playbackUrl = info.media_url;
    clearMarkers();
    st.frameStartMs = -1;
    st.busy = null;

    dom.app.setAttribute("data-state", "ready");
    dom.video.src = info.media_url;
    dom.video.load();
    trackFrames();
    render();
    bumpAutoHide();

    var size = info.width && info.height ? "  ·  " + info.width + "×" + info.height : "";
    var fps = info.fps ? "  ·  " + (Math.round(info.fps * 100) / 100) + " fps" : "";
    toast(info.name + size + fps, 2600);

    if (info.likely_playable === false) {
      offerProxy(
        "This file" + (info.codec ? " (" + info.codec + ")" : "") +
        " may not play in a browser. A preview can be transcoded — clips are " +
        "still cut from the original."
      );
    }
  }

  /* Play a file the user picked, straight from their own disk.
   *
   * Nothing is sent anywhere. A blob URL is the same <video> element and the
   * same A-B rules, so marking, scrubbing and frame stepping all work on a file
   * the host has never seen — which also means a 4 GB source is instant instead
   * of a long upload you might not even want.
   *
   * The host only ever receives what you actually ask it to save: a still, or
   * the source at the moment you ask for a clip. */
  function loadLocalFile(file) {
    if (!file) return;
    if (st.objectUrl) {
      URL.revokeObjectURL(st.objectUrl);
      st.objectUrl = null;
    }

    var url = URL.createObjectURL(file);
    st.objectUrl = url;
    st.localFile = file;
    st.fpsSamples = [];

    st.media = {
      name: file.name,
      local: true,
      token: "",
      duration_ms: 0,
      fps: 0,
      width: 0,
      height: 0,
      codec: ""
    };
    st.playbackUrl = url;
    clearMarkers();
    st.frameStartMs = -1;
    st.busy = null;

    dom.app.setAttribute("data-state", "ready");
    dom.video.src = url;
    dom.video.load();
    trackFrames();
    render();
    bumpAutoHide();
    toast("Playing " + file.name + " from this device — nothing has been sent.", 3200);
  }

  /* The browser could not decode it locally. Cutting still needs the host's
   * ffmpeg, so fall back to sending it and let the server drive playback. */
  function fallBackToUpload(reason) {
    var file = st.localFile;
    if (!file) return false;
    st.localFile = null;
    toast(reason, 0);
    uploadFile(file)
      .then(function (info) {
        hideToast();
        loadMedia(info);
        if (info.likely_playable === false) {
          offerProxy("This file may not play in a browser. A preview can be " +
                     "transcoded — clips are still cut from the original.");
        }
      })
      .catch(function (err) { fail(err.message); });
    return true;
  }

  /* Open a file from the save folder, named relative to it. The path never
   * identifies a location on the host: it is what the Files browser listed, and
   * the server re-resolves it inside the save folder before opening anything. */
  function openLibrary(path, label) {
    var name = label || String(path || "").split("/").pop();
    toast("Opening " + name + "…", 0);
    return postJson("/vt/api/library/open", { path: path })
      .then(function (info) {
        hideToast();
        st.localFile = null;
        loadMedia(info);
        return info;
      })
      .catch(function (err) { fail(err.message); throw err; });
  }

  function openPath(path) {
    var raw = (path || "").trim();
    if (!raw) {
      toast("Paste a path to a video file first.", 3000);
      return;
    }
    toast("Opening " + raw + "…", 0);
    postJson("/vt/api/open", { path: raw })
      .then(function (info) { hideToast(); loadMedia(info); })
      .catch(function (err) { fail(err.message); });
  }

  /* Make sure the host has the source, uploading it now if this is a local
   * file. Resolves with the server-side media info. */
  function ensureOnHost() {
    if (st.media && st.media.token) return Promise.resolve(st.media);
    if (!st.localFile) {
      return Promise.reject(new Error("That video is no longer open."));
    }
    var markers = { a: st.a, b: st.b };
    return uploadFile(st.localFile).then(function (info) {
      hideToast();
      // Keep playing the local copy; the upload exists only so ffmpeg can cut
      // from it. Re-loading the server URL here would restart playback and
      // throw away the markers the user just set.
      st.media.token = info.token;
      st.media.fps = st.media.fps || info.fps;
      st.media.width = st.media.width || info.width;
      st.media.height = st.media.height || info.height;
      st.a = markers.a;
      st.b = markers.b;
      render();
      return info;
    });
  }

  /* Send a blob as the raw request body rather than as a multipart form, so the
   * server can stream it to disk instead of buffering the whole thing first.
   * That is what makes sending a few gigabytes from another machine sane.
   *
   * Every state-changing request in this file goes through here or through
   * request() above, and both attach the CSRF header. Hand-rolling one more XHR
   * is how the header gets forgotten — which is exactly what happened once. */
  function sendBlob(url, blob, options) {
    options = options || {};
    return new Promise(function (resolve, reject) {
      var xhr = new XMLHttpRequest();
      xhr.open("POST", url);
      xhr.setRequestHeader("Content-Type", "application/octet-stream");
      xhr.setRequestHeader("X-VT-CSRF", csrfToken());
      xhr.withCredentials = true;

      if (options.onProgress) {
        xhr.upload.onprogress = function (event) {
          if (!event.lengthComputable) return;
          options.onProgress(Math.round((event.loaded / event.total) * 100));
        };
      }
      if (options.onSent) xhr.upload.onload = options.onSent;

      xhr.onload = function () {
        if (xhr.status === 401) {
          window.location.href = "/login";
          reject(new Error("Login required"));
          return;
        }
        var body = {};
        try { body = JSON.parse(xhr.responseText || "{}"); } catch (err) { /* ignore */ }
        if (xhr.status >= 200 && xhr.status < 300) {
          resolve(body);
        } else {
          reject(new Error(body.detail || ("Request failed (" + xhr.status + ")")));
        }
      };
      xhr.onerror = function () { reject(new Error("The connection dropped.")); };
      xhr.send(blob);
    });
  }

  /* Copy the source to the host. Only reached when something actually needs it
   * there — cutting a clip — because playback and stills are done locally. */
  function uploadFile(file) {
    if (!file) return Promise.reject(new Error("No file."));

    var label = "Sending " + file.name + " to " + hostLabel();
    toast(label + "…", 0);
    return sendBlob("/vt/api/upload?name=" + encodeURIComponent(file.name), file, {
      onProgress: function (pct) { toast(label + "…  " + pct + "%", 0); },
      onSent: function () { toast(label + " — reading it…", 0); }
    });
  }

  // --- browser-friendly preview ---------------------------------------------
  function offerProxy(message) {
    if (!config.ffmpeg) {
      fail(message + " (ffmpeg is missing, so no preview can be built.)");
      return;
    }
    toast(message, 0, { action: { label: "Build preview", run: buildProxy } });
  }

  function buildProxy() {
    if (!hasMedia() || st.busy) return;
    // Only ever reached for media the host already holds; a local file that
    // will not decode takes the upload fallback in onVideoError() instead.
    if (!st.media.token) return;
    postJson("/vt/api/proxy", { token: st.media.token })
      .then(function (job) {
        if (job.state === "done" && job.proxy_url) {
          usePreview(job.proxy_url);
          return;
        }
        st.busy = job.id;
        render();
        pollJob(job.id, "Building preview", function (done) {
          usePreview(done.proxy_url);
        });
      })
      .catch(function (err) { fail(err.message); });
  }

  function usePreview(url) {
    if (!url) return;
    var resume = positionMs();
    st.playbackUrl = url;
    st.busy = null;
    dom.video.src = url;
    dom.video.load();
    dom.video.addEventListener("loadedmetadata", function once() {
      dom.video.removeEventListener("loadedmetadata", once);
      rawSeek(resume);
    });
    trackFrames();
    render();
    toast("Playing a browser-friendly preview. Exports still come from the original.", 4200);
  }

  // --- exports ---------------------------------------------------------------
  function pollJob(jobId, label, onDone) {
    var tick = function () {
      request("/vt/api/job/" + jobId)
        .then(function (job) {
          if (job.state === "done") {
            st.busy = null;
            render();
            if (onDone) onDone(job);
            return;
          }
          if (job.state === "failed" || job.state === "cancelled") {
            st.busy = null;
            render();
            fail(job.error || "The export failed.");
            return;
          }
          toast(label + "…  " + (job.percent || 0) + "%", 0);
          window.setTimeout(tick, JOB_POLL_MS);
        })
        .catch(function (err) {
          st.busy = null;
          render();
          fail(err.message);
        });
    };
    tick();
  }

  /* The two things an A-B range can be saved as. Both are cut on the host from
   * the original file, with the same markers; only the route and the words
   * differ. Audio ignores the gear menu, which is all about the picture. */
  var EXPORTS = {
    video: { route: "/vt/api/clip", doing: "Exporting clip", noun: "the clip",
             options: true },
    audio: { route: "/vt/api/audio", doing: "Exporting audio", noun: "the audio",
             options: false }
  };

  function saveClip() { exportRange("video"); }
  function saveAudio() { exportRange("audio"); }

  function exportRange(kind) {
    var spec = EXPORTS[kind];
    if (!hasMedia()) return;
    if (!hasLoop()) {
      toast("Set an A-B loop first.", 2400);
      return;
    }
    if (st.busy) {
      toast("An export is already running.", 2400);
      return;
    }
    if (!config.ffmpeg) {
      fail("ffmpeg was not found, so nothing can be cut. Install imageio-ffmpeg in the venv and restart.");
      return;
    }

    /* Cutting is the one thing the browser cannot do at full quality, so this
     * is where a local file finally gets sent — and only the first time, and
     * only because you asked for a cut. */
    var a = Math.round(st.a);
    var b = Math.round(st.b);

    if (st.media.local && !st.media.token) {
      toast("Sending the video so it can be cut…", 0);
    } else {
      toast(spec.doing + "…  0%", 0);
    }

    ensureOnHost()
      .then(function () {
        toast(spec.doing + "…  0%", 0);
        var body = { token: st.media.token, a_ms: a, b_ms: b };
        if (spec.options) body.options = optionsPayload();
        return postJson(spec.route, body);
      })
      .then(function (job) {
        st.busy = job.id;
        render();
        pollJob(job.id, spec.doing, function (done) {
          reportOutcome(done, spec.noun);
        });
      })
      .catch(function (err) { fail(err.message); });
  }

  /* Grab the frame on screen as a PNG, in the browser, at the source's own
   * pixel size — a still from a 4K video is 3840×2160 however small the window
   * is. For a local file this means the host receives the still and nothing
   * else; the video itself never leaves the device. */
  function captureFrame() {
    var video = dom.video;
    var width = video.videoWidth;
    var height = video.videoHeight;
    if (!width || !height) {
      return Promise.reject(new Error("There is no frame to capture yet."));
    }
    return new Promise(function (resolve, reject) {
      var canvas = document.createElement("canvas");
      canvas.width = width;
      canvas.height = height;
      try {
        canvas.getContext("2d").drawImage(video, 0, 0, width, height);
      } catch (err) {
        reject(new Error("This video cannot be captured in the browser."));
        return;
      }
      if (!canvas.toBlob) {
        reject(new Error("This browser cannot save a still."));
        return;
      }
      canvas.toBlob(function (blob) {
        if (blob) resolve(blob);
        else reject(new Error("The frame could not be encoded."));
      }, "image/png");
    });
  }

  function saveScreenshot() {
    if (!hasMedia()) return;
    var position = Math.round(positionMs());

    // A local file is captured here and only the PNG is sent. A file already on
    // the host is captured there by ffmpeg, which is exact and costs no upload.
    if (st.media.local) {
      toast("Capturing frame…", 0);
      captureFrame()
        .then(function (blob) {
          toast("Saving the still…", 0);
          return sendBlob(
            "/vt/api/still?name=" + encodeURIComponent(st.media.name) +
            "&position_ms=" + position,
            blob
          );
        })
        .then(function (saved) { reportOutcome(saved, "the still"); })
        .catch(function (err) { fail(err.message); });
      return;
    }

    if (!config.ffmpeg) {
      fail("ffmpeg was not found, so stills cannot be written. Install imageio-ffmpeg in the venv and restart.");
      return;
    }
    toast("Capturing frame…", 0);
    postJson("/vt/api/screenshot", {
      token: st.media.token,
      position_ms: position
    })
      .then(function (saved) { reportOutcome(saved, "the still"); })
      .catch(function (err) { fail(err.message); });
  }

  /* Report what actually happened. "Saved", "skipped" and "a partial may remain"
   * are three different outcomes, and reporting the first when the third is true
   * is how somebody ends up trusting a file that is not right. */
  function reportOutcome(result, fallbackLabel) {
    if (!result) return;
    if (result.status === "already_exists") {
      toast("Already exists — skipped. Nothing was replaced.", 4200);
      return;
    }
    if (result.status === "possible_partial") {
      fail(result.message ||
        "A previous transfer may have left an incomplete file with this name.");
      return;
    }
    noteSaved(result);
    toast("Saved  " + (result.saved_name || fallbackLabel), 3400);
  }

  function noteSaved(saved) {
    if (!saved || !saved.saved_name || !saved.download_url) return;
    st.saved.unshift(saved);
    st.saved = st.saved.slice(0, 6);
    dom.savedDir.textContent = config.destination || "the host's save folder";
    dom.savedList.innerHTML = "";
    st.saved.forEach(function (entry) {
      var item = document.createElement("li");
      var link = document.createElement("a");
      link.href = entry.download_url;
      link.textContent = entry.saved_name;
      link.title = "Download " + entry.saved_name;
      link.setAttribute("download", entry.saved_name);
      item.appendChild(link);
      dom.savedList.appendChild(item);
    });
    dom.saved.hidden = false;
  }

  // --- host file browser -----------------------------------------------------
  function openBrowser(dir) {
    request("/vt/api/browse?dir=" + encodeURIComponent(dir || st.browseDir || ""))
      .then(function (listing) {
        st.browseDir = listing.dir;
        dom.sheet.hidden = false;
        // Keep the tail of a long path — that is the part that identifies it.
        dom.sheetDir.textContent = listing.dir.length > 64
          ? "…" + listing.dir.slice(listing.dir.length - 63)
          : listing.dir;
        dom.sheetDir.title = listing.dir;

        dom.sheetShortcuts.innerHTML = "";
        listing.shortcuts.forEach(function (shortcut) {
          var button = document.createElement("button");
          button.type = "button";
          button.textContent = shortcut.name;
          button.onclick = function () { openBrowser(shortcut.path); };
          dom.sheetShortcuts.appendChild(button);
        });

        dom.sheetList.innerHTML = "";
        var add = function (glyph, label, note, onPick) {
          var item = document.createElement("li");
          var button = document.createElement("button");
          button.type = "button";
          var icon = document.createElement("i");
          icon.innerHTML = glyph;
          var name = document.createElement("span");
          name.textContent = label;
          button.appendChild(icon);
          button.appendChild(name);
          if (note) {
            var meta = document.createElement("em");
            meta.textContent = note;
            button.appendChild(meta);
          }
          button.onclick = onPick;
          item.appendChild(button);
          dom.sheetList.appendChild(item);
        };

        if (listing.parent) {
          add(ICONS.up, "..", "", function () { openBrowser(listing.parent); });
        }
        listing.folders.forEach(function (folder) {
          add(ICONS.folder, folder.name, "", function () { openBrowser(folder.path); });
        });
        listing.files.forEach(function (file) {
          add(ICONS.file, file.name, fmtSize(file.size), function () {
            closeBrowser();
            openPath(file.path);
          });
        });
        if (!listing.folders.length && !listing.files.length && !listing.parent) {
          var empty = document.createElement("li");
          empty.className = "vt-sheet-empty";
          empty.textContent = "Nothing here.";
          dom.sheetList.appendChild(empty);
        }
      })
      .catch(function (err) { fail(err.message); });
  }

  function closeBrowser() {
    dom.sheet.hidden = true;
  }

  // --- frame timing ----------------------------------------------------------
  /* Track each presented frame's own timestamp so stepping is exact. Chrome,
   * Edge and Safari provide it; Firefox falls back to currentTime arithmetic. */
  function trackFrames() {
    var video = dom.video;
    if (!video || typeof video.requestVideoFrameCallback !== "function") return;
    var token = st.playbackUrl;
    var previous = -1;
    var step = function (now, meta) {
      if (st.playbackUrl !== token) return;   // a new source took over
      if (meta && typeof meta.mediaTime === "number") {
        /* A local file has no server-side probe, so the frame rate is measured
         * from the frames themselves: the gap between presented timestamps is
         * one frame. The median of a handful of samples shrugs off the odd
         * dropped or duplicated frame. Until enough have arrived, frameMs()
         * falls back to FALLBACK_FPS, exactly as it does for a file that
         * reports no rate of its own. */
        if (st.media && !st.media.fps && previous >= 0) {
          var delta = meta.mediaTime - previous;
          if (delta > 0.001 && delta < 1) {
            st.fpsSamples.push(delta);
            if (st.fpsSamples.length >= 12) {
              var sorted = st.fpsSamples.slice().sort(function (x, y) { return x - y; });
              st.media.fps = Math.round(1 / sorted[Math.floor(sorted.length / 2)]);
              render();
            }
          }
        }
        previous = meta.mediaTime;
        st.frameStartMs = meta.mediaTime * 1000;
      }
      video.requestVideoFrameCallback(step);
    };
    video.requestVideoFrameCallback(step);
  }

  // --- gestures --------------------------------------------------------------
  function zoneAction(event) {
    var rect = dom.stage.getBoundingClientRect();
    var ratio = rect.width ? (event.clientX - rect.left) / rect.width : 0.5;
    if (ratio < 1 / 3) {
      skip(-SKIP_MS);
      flash("left", "« 5s");
    } else if (ratio > 2 / 3) {
      skip(SKIP_MS);
      flash("right", "5s »");
    } else {
      toggle();
    }
  }

  function onStageTap(event) {
    if (event.button !== undefined && event.button !== 0) return;
    // Clicks on the bar, the sheet or the placeholder links are not video taps.
    if (event.target.closest(".vt-panel, .vt-sheet, .vt-placeholder-hint, .vt-toast")) return;

    // Tapping the video is how you dismiss the options menu, the same way it
    // dismisses the pinned control bar.
    if (st.optionsOpen) {
      toggleOptions(false);
      return;
    }

    if (st.tapTimer) {
      window.clearTimeout(st.tapTimer);
      st.tapTimer = 0;
      if (hasMedia()) zoneAction(event);
      return;
    }
    st.tapTimer = window.setTimeout(function () {
      st.tapTimer = 0;
      // A tap pins the bar up; tapping again dismisses it.
      pinControls(!st.pinned);
    }, DOUBLE_TAP_MS);
  }

  // --- keyboard --------------------------------------------------------------
  function typingInAField() {
    var node = document.activeElement;
    if (!node) return false;
    if (node.isContentEditable) return true;
    return /^(input|textarea|select)$/i.test(node.tagName);
  }

  function onKeyDown(event) {
    if (event.ctrlKey && event.key.toLowerCase() !== "o") return;
    if (event.metaKey || event.altKey) return;

    // The Files browser hosts this player on a page where it is often hidden.
    // A hidden player must not be quietly eating the arrow keys.
    if (playerHidden()) return;

    if (event.key === "Escape") {
      if (st.optionsOpen) { toggleOptions(false); event.preventDefault(); return; }
      if (!dom.sheet.hidden) { closeBrowser(); event.preventDefault(); return; }
      if (document.fullscreenElement) { document.exitFullscreen(); event.preventDefault(); }
      return;
    }
    if (typingInAField()) return;

    var key = event.key;
    var lower = key.length === 1 ? key.toLowerCase() : key;

    if (lower === "o") {
      openBrowser();
      event.preventDefault();
      return;
    }
    if (lower === "g") {
      toggleOptions();
      event.preventDefault();
      return;
    }
    if (!hasMedia()) return;

    switch (lower) {
      case " ":
      case "spacebar":
        toggle();
        break;
      case ",":
      case "<":
        stepFrames(-1);
        break;
      case ".":
      case ">":
        stepFrames(1);
        break;
      case "ArrowLeft":
        if (event.shiftKey) {
          stepFrames(-1);
        } else {
          skip(-SKIP_MS);
          flash("left", "« 5s");
        }
        break;
      case "ArrowRight":
        if (event.shiftKey) {
          stepFrames(1);
        } else {
          skip(SKIP_MS);
          flash("right", "5s »");
        }
        break;
      case "ArrowUp":
        dom.video.volume = Math.min(1, dom.video.volume + 0.05);
        toast("Volume " + Math.round(dom.video.volume * 100) + "%", 1200);
        break;
      case "ArrowDown":
        dom.video.volume = Math.max(0, dom.video.volume - 0.05);
        toast("Volume " + Math.round(dom.video.volume * 100) + "%", 1200);
        break;
      case "Home":
        stop();
        break;
      case "b":
        cycleMarker();
        break;
      case "r":
        st.repeat = !st.repeat;
        render();
        toast(st.repeat ? "Repeat on" : "Repeat off", 1400);
        break;
      case "m":
        dom.video.muted = !dom.video.muted;
        render();
        break;
      case "s":
        saveScreenshot();
        break;
      case "c":
        saveClip();
        break;
      case "a":
        saveAudio();
        break;
      case "f":
      case "F11":
        toggleFullscreen();
        break;
      default:
        return;
    }
    event.preventDefault();
    bumpAutoHide();
  }

  /* True when this player is on a page that is currently showing something
   * else — the Files browser keeps it mounted behind the listing. */
  function playerHidden() {
    if (!dom.app) return true;
    if (document.fullscreenElement) return false;
    return dom.app.offsetParent === null;
  }

  function toggleFullscreen() {
    if (document.fullscreenElement) {
      document.exitFullscreen();
    } else if (dom.stage.requestFullscreen) {
      dom.stage.requestFullscreen().catch(function () {
        toast("This browser would not go fullscreen.", 2600);
      });
    }
  }

  // --- wiring ----------------------------------------------------------------
  var ACTIONS = {
    play: toggle,
    stop: stop,
    back5: function () { skip(-SKIP_MS); flash("left", "« 5s"); },
    fwd5: function () { skip(SKIP_MS); flash("right", "5s »"); },
    "prev-frame": function () { stepFrames(-1); },
    "next-frame": function () { stepFrames(1); },
    repeat: function () {
      st.repeat = !st.repeat;
      render();
      toast(st.repeat ? "Repeat on" : "Repeat off", 1400);
    },
    marker: cycleMarker,
    clip: saveClip,
    audio: saveAudio,
    screenshot: saveScreenshot,
    mute: function () { dom.video.muted = !dom.video.muted; render(); },
    fullscreen: toggleFullscreen,
    // One "get me a video" slot: the host browser here, the upload picker away.
    browse: function () {
      if (st.local) openBrowser(); else dom.fileInput.click();
    },
    pick: function () { dom.fileInput.click(); },
    "sheet-close": closeBrowser,
    options: function () { toggleOptions(); },
    "options-close": function () { toggleOptions(false); },
    "options-reset": resetOptions
  };

  function wire() {
    dom.app.addEventListener("click", function (event) {
      var trigger = event.target.closest("[data-vt]");
      if (!trigger || !dom.app.contains(trigger)) return;
      var action = ACTIONS[trigger.getAttribute("data-vt")];
      if (!action) return;
      event.preventDefault();
      event.stopPropagation();
      action();
      if (!trigger.closest(".vt-sheet")) bumpAutoHide();
    });

    // Scrubbing.
    dom.scrub.addEventListener("pointerdown", function (event) {
      event.preventDefault();
      beginScrub(event);
    });
    dom.scrub.addEventListener("pointermove", moveScrub);
    dom.scrub.addEventListener("pointerup", endScrub);
    dom.scrub.addEventListener("pointercancel", function (event) { endScrub(event); });

    // Using the bar at all keeps it on screen.
    dom.panel.addEventListener("pointerdown", bumpAutoHide);
    dom.panel.addEventListener("pointermove", bumpAutoHide);

    // Gestures on the video itself.
    dom.stage.addEventListener("click", onStageTap);
    dom.stage.addEventListener("mousemove", function (event) {
      if (event.target.closest(".vt-panel")) return;
      bumpAutoHide();
    });

    // Player events.
    dom.video.addEventListener("timeupdate", render);
    dom.video.addEventListener("progress", render);
    dom.video.addEventListener("seeked", render);
    dom.video.addEventListener("volumechange", render);
    dom.video.addEventListener("durationchange", render);
    dom.video.addEventListener("loadedmetadata", function () {
      // Trust the container when ffmpeg could not read a duration — and for a
      // local file this is the only source of it, because nothing probed it.
      if (st.media && !st.media.duration_ms && isFinite(dom.video.duration)) {
        st.media.duration_ms = Math.round(dom.video.duration * 1000);
      }
      if (st.media && st.media.local) {
        st.media.width = st.media.width || dom.video.videoWidth;
        st.media.height = st.media.height || dom.video.videoHeight;
      }
      render();
    });
    dom.video.addEventListener("ended", function () {
      stopBoundsWatch();
      onEnded();
    });
    dom.video.addEventListener("play", function () {
      stopBoundsWatch();
      st.boundsTimer = window.setInterval(enforceBounds, BOUNDS_TICK_MS);
      render();
      bumpAutoHide();
    });
    dom.video.addEventListener("pause", function () {
      stopBoundsWatch();
      render();
      // Paused controls never auto-hide.
      showPanel();
      window.clearTimeout(st.hideTimer);
    });
    dom.video.addEventListener("error", onVideoError);

    // Drag and drop, and the upload picker.
    ["dragenter", "dragover"].forEach(function (name) {
      dom.stage.addEventListener(name, function (event) {
        if (!event.dataTransfer) return;
        event.preventDefault();
        event.dataTransfer.dropEffect = "copy";
        dom.stage.classList.add("vt-dragging");
      });
    });
    ["dragleave", "dragend"].forEach(function (name) {
      dom.stage.addEventListener(name, function () {
        dom.stage.classList.remove("vt-dragging");
      });
    });
    dom.stage.addEventListener("drop", function (event) {
      event.preventDefault();
      dom.stage.classList.remove("vt-dragging");
      var files = event.dataTransfer && event.dataTransfer.files;
      if (files && files.length) {
        loadLocalFile(files[0]);
        return;
      }
      // Dragging from a file manager sometimes yields a path instead.
      var text = event.dataTransfer ? event.dataTransfer.getData("text/plain") : "";
      if (text) openPath(text.replace(/^file:\/\//, ""));
    });
    dom.fileInput.addEventListener("change", function () {
      if (dom.fileInput.files && dom.fileInput.files.length) {
        loadLocalFile(dom.fileInput.files[0]);
        dom.fileInput.value = "";
      }
    });

    dom.sheet.addEventListener("click", function (event) {
      if (event.target === dom.sheet) closeBrowser();
    });

    // Gradio can re-render the HTML block, which re-mounts us against fresh
    // nodes. Those get fresh listeners, but the document keeps the ones it has —
    // binding twice would make every shortcut fire twice.
    if (!st.docWired) {
      st.docWired = true;
      document.addEventListener("keydown", onKeyDown);
      document.addEventListener("fullscreenchange", function () { render(); });
    }
  }

  /* Publish the control bar's height so the toast can sit above it whatever it
   * wraps to, and the stage's so the options menu can size itself to the room
   * that actually leaves. The stage clips its children, so a menu that guessed
   * would be a menu with its top cut off. */
  function watchPanelHeight() {
    var publish = function () {
      var height = Math.round(dom.panel.getBoundingClientRect().height);
      if (height > 0) dom.stage.style.setProperty("--vt-panel-height", height + "px");
      var stage = Math.round(dom.stage.getBoundingClientRect().height);
      if (stage > 0) dom.stage.style.setProperty("--vt-stage-height", stage + "px");
    };
    publish();
    if (typeof ResizeObserver === "function") {
      var observer = new ResizeObserver(publish);
      observer.observe(dom.panel);
      observer.observe(dom.stage);
    } else {
      window.addEventListener("resize", publish);
    }
  }

  function onVideoError() {
    var error = dom.video.error;
    if (!error || !st.media) return;
    // 4 = the container/codec is not supported, 3 = it decoded badly.
    var undecodable = error.code === 4 || error.code === 3;

    /* A local file the browser cannot decode is the one case where playing it
     * here is not an option — MKV and HEVC are the usual culprits. ffmpeg can
     * still read it, so send it after all and say why, rather than leaving a
     * black frame and a file that "does not work". */
    if (undecodable && st.media.local) {
      fallBackToUpload(
        "Your browser cannot play this file, so it is being sent to " +
        hostLabel() + " where ffmpeg can read it…"
      );
      return;
    }

    if (undecodable) {
      offerProxy(
        "Your browser cannot play this file" +
        (st.media.codec ? " (" + st.media.codec + ")" : "") +
        ". A preview can be transcoded — clips are still cut from the original."
      );
    } else {
      fail("Playback failed: " + (error.message || ("media error " + error.code)));
    }
  }

  // --- mounting --------------------------------------------------------------
  function collect() {
    var byId = function (id) { return document.getElementById(id); };
    dom.app = byId("vt-app");
    if (!dom.app) return false;
    dom.stage = byId("vt-stage");
    dom.video = byId("vt-video");
    dom.panel = byId("vt-panel");
    dom.scrub = byId("vt-scrub");
    dom.buffer = byId("vt-buffer");
    dom.played = byId("vt-played");
    dom.abFill = byId("vt-ab-fill");
    dom.markA = byId("vt-mark-a");
    dom.markB = byId("vt-mark-b");
    dom.handle = byId("vt-handle");
    dom.pos = byId("vt-pos");
    dom.dur = byId("vt-dur");
    dom.timeAb = byId("vt-time-ab");
    dom.toast = byId("vt-toast");
    dom.flashLeft = byId("vt-flash-left");
    dom.flashRight = byId("vt-flash-right");
    dom.sheet = byId("vt-sheet");
    dom.sheetDir = byId("vt-sheet-dir");
    dom.sheetShortcuts = byId("vt-sheet-shortcuts");
    dom.sheetList = byId("vt-sheet-list");
    dom.saved = byId("vt-saved");
    dom.savedDir = byId("vt-saved-dir");
    dom.savedList = byId("vt-saved-list");
    dom.fileInput = byId("vt-file-input");
    dom.outputNote = byId("vt-output-note");

    dom.options = byId("vt-options");
    dom.optSource = byId("vt-opt-source");
    dom.optScales = byId("vt-opt-scales");
    dom.optWidth = byId("vt-opt-width");
    dom.optHeight = byId("vt-opt-height");
    dom.optCrf = byId("vt-opt-crf");
    dom.optCrfLabel = byId("vt-opt-crf-label");
    dom.optPreset = byId("vt-opt-preset");
    dom.optFps = byId("vt-opt-fps");
    dom.optAudio = byId("vt-opt-audio");
    dom.estFull = byId("vt-est-full");
    dom.estRange = byId("vt-est-range");
    dom.estRangeLabel = byId("vt-est-range-label");
    dom.estNote = byId("vt-est-note");

    // Scoped to the panel on purpose: "browse" also names the placeholder link,
    // and an unscoped query would hand back that one instead of the button.
    dom.btn = {};
    ["play", "stop", "back5", "fwd5", "repeat", "marker", "clip", "audio", "screenshot",
     "mute", "fullscreen", "browse", "options"].forEach(function (name) {
      dom.btn[name] = dom.panel.querySelector('[data-vt="' + name + '"]');
    });
    dom.btn.prevFrame = dom.panel.querySelector('[data-vt="prev-frame"]');
    dom.btn.nextFrame = dom.panel.querySelector('[data-vt="next-frame"]');

    // The toast holds its text and an optional action button.
    dom.toast.innerHTML = '<span class="vt-toast-text"></span>' +
      '<button type="button" class="vt-toast-action" hidden></button>';
    dom.toastText = dom.toast.querySelector(".vt-toast-text");
    dom.toastAction = dom.toast.querySelector(".vt-toast-action");
    return true;
  }

  function mount() {
    if (st.mounted && dom.app && document.body.contains(dom.app)) return;
    if (!collect()) {
      window.setTimeout(mount, 120);
      return;
    }
    stopBoundsWatch();
    st.mounted = true;
    paintIcons(dom.app);
    wire();
    watchPanelHeight();
    render();
    showPanel();

    loadStoredOptions();
    buildOptionsUI();

    request("/vt/api/config")
      .then(function (data) {
        config = data;
        if (data.export_model) {
          // Repaint against the server's own choices rather than the built-in
          // stand-in, then re-apply whatever was saved on this device.
          MODEL = data.export_model;
          st.options = null;
          loadStoredOptions();
          buildOptionsUI();
        }
        // Browsing host paths, not being the host: the two are different
        // questions and only the first one decides what this page offers.
        st.local = data.can_browse !== false;
        dom.savedDir.textContent = data.destination || "the host's save folder";
        applyReach();
        if (!data.ffmpeg) {
          fail("ffmpeg was not found — playback works, but nothing can be exported.");
        }
        render();
      })
      .catch(function () { /* the UI still works without the config */ });

    // `python webui.py <file>` opens the browser with ?open=… on it.
    var wanted = new URLSearchParams(window.location.search).get("open");
    if (wanted && !hasMedia()) openPath(wanted);
  }

  window.VideoTrim = {
    mount: mount,
    openPath: openPath,
    openLibrary: openLibrary,
    openBrowser: openBrowser,
    closeOptions: function () { toggleOptions(false); },
    pause: pause,
    state: st
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", mount);
  } else {
    mount();
  }
})();
