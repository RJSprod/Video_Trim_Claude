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
 * Exports are not done here: the clip and the still are cut by ffmpeg on the
 * server, from the original file, and land on that machine's Desktop.
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
  var config = { output_dir: "", ffmpeg: true };

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
    clip: svg('<path d="M10 3.4v7.8M6.6 8.4L10 11.8l3.4-3.4M4.2 13.6v2.2a1 1 0 0 0 1 1h9.6a1 1 0 0 0 1-1v-2.2"/>', true),
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
    browseDir: ""
  };

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
    dom.btn.screenshot.disabled = !hasMedia() || !config.ffmpeg;

    ["stop", "back5", "fwd5", "prevFrame", "nextFrame", "marker", "repeat"].forEach(function (key) {
      if (dom.btn[key]) dom.btn[key].disabled = !hasMedia();
    });
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
  function request(url, options) {
    return fetch(url, options).then(function (response) {
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
   * The WebUI is served to the whole network, but reading paths on the host is
   * refused for anyone not sitting at it. Rather than let a remote visitor type
   * a path and collect a 403, the path row and the folder browser are taken away
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

    if (dom.outputNote) {
      dom.outputNote.textContent = local
        ? "Clips and stills are written to " + config.output_dir + " on this machine."
        : "Uploads are trimmed on " + hostLabel() + ", and clips and stills are " +
          "written to its Desktop (" + config.output_dir + "). Each one is offered " +
          "here as a download too.";
    }
  }

  function hostLabel() {
    return window.location.hostname || "the host";
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

  /* Send the file as the raw request body rather than as a multipart form, so
   * the server can stream it to disk instead of buffering the whole thing first.
   * That is what makes uploading a few gigabytes from another machine sane. */
  function uploadFile(file) {
    if (!file) return;

    var xhr = new XMLHttpRequest();
    xhr.open("POST", "/vt/api/upload?name=" + encodeURIComponent(file.name));
    xhr.setRequestHeader("Content-Type", "application/octet-stream");

    var label = "Sending " + file.name + " to " + hostLabel();
    xhr.upload.onprogress = function (event) {
      if (!event.lengthComputable) return;
      var pct = Math.round((event.loaded / event.total) * 100);
      toast(label + "…  " + pct + "%", 0);
    };
    xhr.upload.onload = function () {
      toast(label + " — reading it…", 0);
    };
    xhr.onload = function () {
      var body = {};
      try { body = JSON.parse(xhr.responseText || "{}"); } catch (err) { /* ignore */ }
      if (xhr.status >= 200 && xhr.status < 300) {
        hideToast();
        loadMedia(body);
      } else {
        fail(body.detail || ("Upload failed (" + xhr.status + ")"));
      }
    };
    xhr.onerror = function () { fail("Upload failed — the connection dropped."); };
    toast(label + "…", 0);
    xhr.send(file);
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

  function saveClip() {
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
      fail("ffmpeg was not found, so clips cannot be cut. Install imageio-ffmpeg in the venv and restart.");
      return;
    }

    toast("Exporting clip…  0%", 0);
    postJson("/vt/api/clip", {
      token: st.media.token,
      a_ms: Math.round(st.a),
      b_ms: Math.round(st.b)
    })
      .then(function (job) {
        st.busy = job.id;
        render();
        pollJob(job.id, "Exporting clip", function (done) {
          noteSaved(done);
          toast("Saved  " + done.saved_name, 3400);
        });
      })
      .catch(function (err) { fail(err.message); });
  }

  function saveScreenshot() {
    if (!hasMedia()) return;
    if (!config.ffmpeg) {
      fail("ffmpeg was not found, so stills cannot be written. Install imageio-ffmpeg in the venv and restart.");
      return;
    }
    toast("Capturing frame…", 0);
    postJson("/vt/api/screenshot", {
      token: st.media.token,
      position_ms: Math.round(positionMs())
    })
      .then(function (saved) {
        noteSaved(saved);
        toast("Saved  " + saved.saved_name, 2800);
      })
      .catch(function (err) { fail(err.message); });
  }

  function noteSaved(saved) {
    if (!saved || !saved.saved_name) return;
    st.saved.unshift(saved);
    st.saved = st.saved.slice(0, 6);
    dom.savedDir.textContent = saved.saved_dir || config.output_dir;
    dom.savedList.innerHTML = "";
    st.saved.forEach(function (entry) {
      var item = document.createElement("li");
      var link = document.createElement("a");
      link.href = entry.download_url;
      link.textContent = entry.saved_name;
      link.title = "Download " + entry.saved_path;
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
    var step = function (now, meta) {
      if (st.playbackUrl !== token) return;   // a new source took over
      if (meta && typeof meta.mediaTime === "number") {
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

    if (event.key === "Escape") {
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
    screenshot: saveScreenshot,
    mute: function () { dom.video.muted = !dom.video.muted; render(); },
    fullscreen: toggleFullscreen,
    // One "get me a video" slot: the host browser here, the upload picker away.
    browse: function () {
      if (st.local) openBrowser(); else dom.fileInput.click();
    },
    pick: function () { dom.fileInput.click(); },
    "sheet-close": closeBrowser
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
      // Trust the container when ffmpeg could not read a duration.
      if (st.media && !st.media.duration_ms && isFinite(dom.video.duration)) {
        st.media.duration_ms = Math.round(dom.video.duration * 1000);
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
        uploadFile(files[0]);
        return;
      }
      // Dragging from a file manager sometimes yields a path instead.
      var text = event.dataTransfer ? event.dataTransfer.getData("text/plain") : "";
      if (text) openPath(text.replace(/^file:\/\//, ""));
    });
    dom.fileInput.addEventListener("change", function () {
      if (dom.fileInput.files && dom.fileInput.files.length) {
        uploadFile(dom.fileInput.files[0]);
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
   * wraps to. Cheap, and it means one CSS rule covers every viewport width. */
  function watchPanelHeight() {
    var publish = function () {
      var height = Math.round(dom.panel.getBoundingClientRect().height);
      if (height > 0) dom.stage.style.setProperty("--vt-panel-height", height + "px");
    };
    publish();
    if (typeof ResizeObserver === "function") {
      new ResizeObserver(publish).observe(dom.panel);
    } else {
      window.addEventListener("resize", publish);
    }
  }

  function onVideoError() {
    var error = dom.video.error;
    if (!error || !st.media) return;
    // 4 = the container/codec is not supported, 3 = it decoded badly. Either
    // way ffmpeg can still read the file, so offer a transcoded preview.
    if (error.code === 4 || error.code === 3) {
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

    // Scoped to the panel on purpose: "browse" also names the placeholder link,
    // and an unscoped query would hand back that one instead of the button.
    dom.btn = {};
    ["play", "stop", "back5", "fwd5", "repeat", "marker", "clip", "screenshot",
     "mute", "fullscreen", "browse"].forEach(function (name) {
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

    request("/vt/api/config")
      .then(function (data) {
        config = data;
        st.local = data.is_local !== false;
        dom.savedDir.textContent = data.output_dir;
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
    openBrowser: openBrowser,
    state: st
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", mount);
  } else {
    mount();
  }
})();
