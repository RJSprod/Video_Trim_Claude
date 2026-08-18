"""The application shell: what tools exist, who may see them, and the pages.

Home is built from server-reported capabilities rather than from what the
browser thinks it should draw. Hiding a card is a courtesy; the route behind it
refuses on its own regardless, so a crafted URL gets a 403 rather than a
surprise.
"""

import html

from ..security.auth import TRANSPORT_WARNING

PRODUCT_NAME = "Video Trim"
PURPOSE_LINE = "A quiet place to trim, capture and move your media."


class Tool:
    """One card on Home. Adding a mode is adding an entry to TOOLS."""

    def __init__(self, tool_id, title, description, icon, route,
                 required_capability=None, enabled=True):
        self.id = tool_id
        self.title = title
        self.description = description
        self.icon = icon
        self.route = route
        self.required_capability = required_capability
        self.enabled = enabled

    def as_dict(self, capabilities):
        available = self.enabled
        if self.required_capability:
            available = available and bool(capabilities.get(self.required_capability))
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "icon": self.icon,
            "route": self.route,
            "enabled": available,
        }


VIDEO_TRIM_ROUTE = "/tools/video-trim"
TRANSFER_ROUTE = "/tools/media-transfer"
FILES_ROUTE = "/tools/files"
SETTINGS_ROUTE = "/settings"

TOOLS = [
    Tool(
        "video_trim",
        "Video Trim",
        "Mark an A-B loop, watch it repeat, then save that exact span or a still.",
        "film",
        VIDEO_TRIM_ROUTE,
    ),
    Tool(
        "media_transfer",
        "Media Transfer",
        "Send photos and videos from this device to the host's save folder.",
        "send",
        TRANSFER_ROUTE,
    ),
    Tool(
        "files",
        "Files",
        "Browse the save folder, look through the pictures, open a video to trim.",
        "grid",
        FILES_ROUTE,
    ),
    Tool(
        "settings",
        "Settings",
        "Account, save location, and which devices may write to this machine.",
        "sliders",
        SETTINGS_ROUTE,
        required_capability="is_host_admin",
    ),
    Tool(
        "tbd",
        "More soon",
        "Another tool will live here.",
        "spark",
        "",
        enabled=False,
    ),
]


def settings_sections_for(host_admin):
    """Which Settings sections this client actually has.

    Deliberately computed rather than hardcoded as "remote means none", so a
    future remote-safe preference can make the card appear without exposing any
    host-only section.
    """
    if not host_admin:
        return []
    return ["account", "save_location", "ip_access", "maintenance"]


def capabilities(session, host_admin, can_browse, can_write, settings, journal=None,
                 pending_requests=0, partial_notices=0):
    """The whole authenticated client picture, in one response.

    ``output_path`` is present only for host-local sessions. Everyone gets
    ``output_display_name``, which is a label and never a filesystem path — the
    same rule applies to every string this shell produces.
    """
    sections = settings_sections_for(host_admin)
    payload = {
        "username": session.username if session else "",
        "is_host_admin": bool(host_admin),
        "can_browse_host_paths": bool(can_browse),
        "can_write": bool(can_write.allowed),
        "write_reason": can_write.reason,
        "can_request_access": bool(can_write.can_request),
        "access_requested": bool(can_write.requested),
        "settings_count": len(sections),
        "settings_sections": sections,
        "output_configured": settings.is_configured(),
        "output_display_name": settings.destination_for(False),
        "transport_warning": TRANSPORT_WARNING,
    }
    if host_admin:
        payload["output_path"] = settings.destination_for(True)
        payload["pending_access_requests"] = int(pending_requests)
        payload["partial_notices"] = int(partial_notices)

    payload["tools"] = [tool.as_dict(payload) for tool in TOOLS]
    return payload


# --- page rendering ----------------------------------------------------------
_PAGE = """<!DOCTYPE html>
<html lang="en" class="vt-shell">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover" />
<meta name="color-scheme" content="dark light" />
<title>{title}</title>
<link rel="stylesheet" href="/vt/static/{stylesheet}?v={version}" />
{styles}
</head>
<body class="vt-body" data-view="{view}">
{body}
<script src="/vt/static/{script}?v={version}" defer></script>
{scripts}
</body>
</html>
"""


def render_page(title, view, body, version, stylesheet="shell.css", script="shell.js",
                extra_styles=(), extra_scripts=()):
    """One page shape for every view.

    ``extra_styles``/``extra_scripts`` name assets from ``/vt/static`` only —
    they are asset filenames, never URLs, so a page cannot pull in anything this
    application does not itself ship.
    """
    version = html.escape(str(version))
    styles = "\n".join(
        f'<link rel="stylesheet" href="/vt/static/{html.escape(name)}?v={version}" />'
        for name in extra_styles
    )
    scripts = "\n".join(
        f'<script src="/vt/static/{html.escape(name)}?v={version}" defer></script>'
        for name in extra_scripts
    )
    return _PAGE.format(
        title=html.escape(title),
        view=html.escape(view),
        body=body,
        version=version,
        stylesheet=stylesheet,
        script=script,
        styles=styles,
        scripts=scripts,
    )


def login_page(version, message=""):
    """The only unauthenticated surface in the application."""
    note = f'<p class="vt-login-error" role="alert">{html.escape(message)}</p>' if message else ""
    body = f"""
<main class="vt-login">
  <div class="vt-glass vt-login-card">
    <h1 class="vt-login-title">{html.escape(PRODUCT_NAME)}</h1>
    <p class="vt-login-sub">Sign in to continue.</p>
    {note}
    <form class="vt-form" id="vt-login-form" method="post" action="/api/login" autocomplete="on">
      <label class="vt-field">
        <span>Username</span>
        <input type="text" name="username" id="vt-username" autocomplete="username"
               autocapitalize="none" autocorrect="off" required />
      </label>
      <label class="vt-field">
        <span>Password</span>
        <input type="password" name="password" id="vt-password"
               autocomplete="current-password" required />
      </label>
      <button type="submit" class="vt-button vt-button-primary">Sign in</button>
      <p class="vt-status" id="vt-login-status" role="status" aria-live="polite"></p>
    </form>
  </div>
  <p class="vt-transport-note">{html.escape(TRANSPORT_WARNING)}</p>
</main>
"""
    return render_page(f"Sign in — {PRODUCT_NAME}", "login", body, version,
                       stylesheet="login.css", script="login.js")


def home_page(version):
    """Sparse by design: a title, a line, and the cards. Nothing technical.

    No save path, no host addresses, no IP history, no job diagnostics — Home is
    the one screen everyone sees, including sessions that must never learn a host
    filesystem path.
    """
    body = f"""
<div class="vt-shell-frame">
  {_header(home=False)}
  <main class="vt-home" id="vt-home">
    <header class="vt-home-head">
      <h1>{html.escape(PRODUCT_NAME)}</h1>
      <p>{html.escape(PURPOSE_LINE)}</p>
    </header>
    <ul class="vt-cards" id="vt-cards" aria-label="Tools"></ul>
  </main>
</div>
"""
    return render_page(PRODUCT_NAME, "home", body, version)


def transfer_page(version):
    body = f"""
<div class="vt-shell-frame">
  {_header(home=True, title="Media Transfer")}
  <main class="vt-tool vt-transfer" id="vt-transfer">
    <div class="vt-glass vt-panel-card">
      <h2 class="vt-panel-title">Send media to the host</h2>
      <p class="vt-panel-sub" id="vt-transfer-destination">Checking…</p>

      <div class="vt-denied" id="vt-transfer-denied" hidden>
        <p class="vt-denied-text" id="vt-denied-text">File transfer disabled by host</p>
        <button type="button" class="vt-button" id="vt-request-access">Request access</button>
        <p class="vt-status" id="vt-request-status" role="status" aria-live="polite"></p>
      </div>

      <div class="vt-picker" id="vt-transfer-picker" hidden>
        <button type="button" class="vt-button vt-button-primary vt-button-large"
                id="vt-transfer-pick">Choose photos or videos…</button>
        <input type="file" id="vt-transfer-input" accept="image/*,video/*" multiple hidden />
        <p class="vt-hint">Files are copied to the host. Nothing already there is
          replaced — a name that already exists is skipped.</p>
      </div>

      <ul class="vt-transfer-list" id="vt-transfer-list" aria-live="polite"></ul>
    </div>
  </main>
</div>
"""
    return render_page(f"Media Transfer — {PRODUCT_NAME}", "transfer", body, version)


def settings_page(version):
    body = f"""
<div class="vt-shell-frame">
  {_header(home=True, title="Settings")}
  <main class="vt-tool vt-settings" id="vt-settings">
    <section class="vt-glass vt-panel-card" id="vt-section-account">
      <h2 class="vt-panel-title">Account</h2>
      <p class="vt-panel-sub">Signed in as <strong id="vt-account-user">…</strong></p>
      <form class="vt-form" id="vt-account-form">
        <label class="vt-field"><span>Current password</span>
          <input type="password" id="vt-account-current" autocomplete="current-password" required /></label>
        <label class="vt-field"><span>New username</span>
          <input type="text" id="vt-account-username" autocomplete="username"
                 autocapitalize="none" autocorrect="off" required /></label>
        <label class="vt-field"><span>New password</span>
          <input type="password" id="vt-account-password" autocomplete="new-password" required /></label>
        <button type="submit" class="vt-button vt-button-primary">Update account</button>
        <p class="vt-status" id="vt-account-status" role="status" aria-live="polite"></p>
      </form>
    </section>

    <section class="vt-glass vt-panel-card" id="vt-section-save">
      <h2 class="vt-panel-title">Save location</h2>
      <p class="vt-panel-sub">Everything Video Trim saves is created here, and nothing
        already in it is ever changed.</p>
      <p class="vt-path" id="vt-save-current">…</p>
      <div class="vt-row-actions">
        <button type="button" class="vt-button" id="vt-save-browse">Browse…</button>
      </div>
      <form class="vt-form vt-inline" id="vt-save-form">
        <label class="vt-field"><span>Folder</span>
          <input type="text" id="vt-save-path" spellcheck="false" /></label>
        <button type="submit" class="vt-button vt-button-primary">Use this folder</button>
      </form>
      <label class="vt-field"><span>Name shown to other devices</span>
        <input type="text" id="vt-save-label" maxlength="60" /></label>
      <button type="button" class="vt-button" id="vt-save-label-apply">Save name</button>
      <p class="vt-status" id="vt-save-status" role="status" aria-live="polite"></p>
      <div class="vt-browser" id="vt-save-browser" hidden>
        <p class="vt-browser-dir" id="vt-browser-dir"></p>
        <ul class="vt-browser-list" id="vt-browser-list"></ul>
      </div>
    </section>

    <section class="vt-glass vt-panel-card" id="vt-section-ip">
      <h2 class="vt-panel-title">Connected devices</h2>
      <p class="vt-panel-sub">Permission is tied to this device's network address. If the
        address changes, the host will need to allow it again.</p>
      <ul class="vt-ip-list" id="vt-ip-list"></ul>
      <p class="vt-hint" id="vt-ip-cap"></p>
    </section>

    <section class="vt-glass vt-panel-card" id="vt-section-maintenance">
      <h2 class="vt-panel-title">Maintenance notices</h2>
      <p class="vt-panel-sub">Possible partial files from an interrupted run. Video Trim
        reports these and never deletes, repairs, or overwrites them.</p>
      <ul class="vt-notice-list" id="vt-notice-list"></ul>
    </section>

    <p class="vt-transport-note">{html.escape(TRANSPORT_WARNING)}</p>
  </main>
</div>
"""
    return render_page(f"Settings — {PRODUCT_NAME}", "settings", body, version)


# --- the player, and the pages that host it ----------------------------------
# The player's markup lives here rather than in server.py because two pages need
# it now: the Gradio Video Trim tool, and the Files browser, which swaps it in
# when you open a video. One copy means one set of ids for player.js to find.
PLAYER_MARKUP = """
<div id="vt-app" class="vt-app" data-state="empty">
  <div class="vt-stage" id="vt-stage" tabindex="0">
    <video id="vt-video" class="vt-video" playsinline preload="metadata"></video>

    <div class="vt-placeholder" id="vt-placeholder">
      <div class="vt-placeholder-mark" data-icon="play"></div>
      <p class="vt-placeholder-title" id="vt-placeholder-title">Drop a video here</p>
      <p class="vt-placeholder-hint">
        <button type="button" class="vt-upload" data-vt="pick">Choose a video…</button>
      </p>
      <p class="vt-placeholder-hint vt-placeholder-local" id="vt-placeholder-local">
        or <button type="button" class="vt-link" data-vt="browse">browse this machine</button>
        for a file already on it
      </p>
      <p class="vt-placeholder-note" id="vt-output-note"></p>
    </div>

    <div class="vt-flash vt-flash-left" id="vt-flash-left"><span>&laquo; 5s</span></div>
    <div class="vt-flash vt-flash-right" id="vt-flash-right"><span>5s &raquo;</span></div>
    <div class="vt-toast" id="vt-toast"></div>

    <div class="vt-panel" id="vt-panel">
      <!-- The options menu lives inside the control bar on purpose: the gear and
           the menu it opens come and go with the controls, and neither can be on
           screen while the bar is hidden. -->
      <div class="vt-options" id="vt-options" role="dialog" aria-label="Export options" hidden>
        <header class="vt-options-head">
          <span>Export options</span>
          <button type="button" class="vt-options-close" data-vt="options-close"
                  title="Close (Esc)" aria-label="Close export options">&times;</button>
        </header>

        <div class="vt-options-body">
          <section class="vt-option">
            <h4>Frame size <em id="vt-opt-source">&nbsp;</em></h4>
            <div class="vt-option-scales" id="vt-opt-scales" role="group"
                 aria-label="Scale"></div>
            <div class="vt-option-dims">
              <label class="vt-option-field"><span>Width</span>
                <input type="number" id="vt-opt-width" inputmode="numeric" step="2" /></label>
              <span class="vt-option-lock" id="vt-opt-lock" aria-hidden="true"></span>
              <label class="vt-option-field"><span>Height</span>
                <input type="number" id="vt-opt-height" readonly tabindex="-1" /></label>
            </div>
            <p class="vt-option-hint">Locked to the source's aspect ratio, and never
              scaled up past it.</p>
          </section>

          <section class="vt-option">
            <h4>Compression <em id="vt-opt-crf-label">&nbsp;</em></h4>
            <input type="range" id="vt-opt-crf" class="vt-option-range" />
            <div class="vt-option-ends"><span>Bigger file</span><span>Smaller file</span></div>
          </section>

          <section class="vt-option">
            <h4>Encoder speed</h4>
            <select id="vt-opt-preset" class="vt-option-select"></select>
            <p class="vt-option-hint">Slower spends longer looking for savings, and
              lands a smaller file at the same quality.</p>
          </section>

          <section class="vt-option vt-option-pair">
            <div>
              <h4>Frame rate</h4>
              <select id="vt-opt-fps" class="vt-option-select"></select>
            </div>
            <div>
              <h4>Audio</h4>
              <select id="vt-opt-audio" class="vt-option-select"></select>
            </div>
          </section>
        </div>

        <footer class="vt-options-foot">
          <div class="vt-estimate">
            <div class="vt-estimate-row">
              <span>Whole video</span><strong id="vt-est-full">&mdash;</strong>
            </div>
            <div class="vt-estimate-row vt-estimate-range">
              <span id="vt-est-range-label">A-B range</span><strong id="vt-est-range">&mdash;</strong>
            </div>
            <p class="vt-option-hint" id="vt-est-note">About this big at these settings —
              an estimate, not a promise.</p>
          </div>
          <button type="button" class="vt-options-reset" data-vt="options-reset">
            Reset to defaults</button>
        </footer>
      </div>

      <div class="vt-scrub" id="vt-scrub" role="slider" aria-label="Seek"
           aria-valuemin="0" aria-valuenow="0" aria-valuemax="0" tabindex="-1">
        <div class="vt-track">
          <div class="vt-buffer" id="vt-buffer"></div>
          <div class="vt-ab-fill" id="vt-ab-fill"></div>
          <div class="vt-played" id="vt-played"></div>
          <div class="vt-mark vt-mark-a" id="vt-mark-a"><span>A</span></div>
          <div class="vt-mark vt-mark-b" id="vt-mark-b"><span>B</span></div>
          <div class="vt-handle" id="vt-handle"></div>
        </div>
      </div>

      <div class="vt-row">
        <div class="vt-time">
          <div class="vt-time-main"><span id="vt-pos">0:00.0</span><i>/</i><span id="vt-dur">0:00</span></div>
          <div class="vt-time-ab" id="vt-time-ab">no A-B range</div>
        </div>

        <div class="vt-buttons">
          <button type="button" class="vt-btn" data-vt="stop" data-icon="stop"
                  title="Stop — back to A (Home)"></button>
          <button type="button" class="vt-btn" data-vt="back5" data-icon="back5"
                  title="Back 5 seconds (&larr;)"></button>
          <button type="button" class="vt-btn" data-vt="prev-frame" data-icon="prevFrame"
                  title="Previous frame (,)"></button>
          <button type="button" class="vt-btn vt-btn-primary" data-vt="play" data-icon="play"
                  title="Play / pause (Space)"></button>
          <button type="button" class="vt-btn" data-vt="next-frame" data-icon="nextFrame"
                  title="Next frame (.)"></button>
          <button type="button" class="vt-btn" data-vt="fwd5" data-icon="fwd5"
                  title="Forward 5 seconds (&rarr;)"></button>
          <button type="button" class="vt-btn" data-vt="repeat" data-icon="repeat"
                  title="Repeat the A-B range (R)"></button>
          <button type="button" class="vt-btn vt-btn-ab" data-vt="marker"
                  title="Cycle the A-B markers (B)">A-B</button>
          <button type="button" class="vt-btn" data-vt="clip" data-icon="clip"
                  title="Save the A-B clip (C)" disabled></button>
          <button type="button" class="vt-btn" data-vt="screenshot" data-icon="camera"
                  title="Save this frame (S)"></button>
          <button type="button" class="vt-btn" data-vt="options" data-icon="gear"
                  title="Export options (G)" aria-haspopup="dialog"
                  aria-expanded="false"></button>
          <button type="button" class="vt-btn" data-vt="mute" data-icon="volumeOn"
                  title="Mute (M)"></button>
          <button type="button" class="vt-btn" data-vt="fullscreen" data-icon="fullscreen"
                  title="Fullscreen (F)"></button>
          <button type="button" class="vt-btn" data-vt="browse" data-icon="folder"
                  title="Open a video (O)"></button>
        </div>
      </div>
    </div>

    <div class="vt-sheet" id="vt-sheet" hidden>
      <div class="vt-sheet-card">
        <header>
          <span id="vt-sheet-dir">&nbsp;</span>
          <button type="button" class="vt-sheet-close" data-vt="sheet-close" title="Close">&times;</button>
        </header>
        <nav id="vt-sheet-shortcuts"></nav>
        <ul id="vt-sheet-list"></ul>
      </div>
    </div>
  </div>

  <div class="vt-saved" id="vt-saved" hidden>
    <span class="vt-saved-label">Saved to <code id="vt-saved-dir"></code> &mdash; download:</span>
    <ul id="vt-saved-list"></ul>
  </div>

  <input type="file" id="vt-file-input" accept="video/*" hidden />
</div>
"""


def files_page(version):
    """The file browser, plus the player it hands a video to.

    Both live on one page so opening a video is a swap rather than a navigation:
    the way back to the list is a button, not the browser's history, and the
    listing is still there when you return.

    Nothing here is a filesystem path. Entries are named relative to the save
    folder, and the folder itself is called whatever the server decided this
    session may be told — a real path for the host, a label for everyone else.
    """
    body = f"""
<div class="vt-shell-frame vt-files-frame">
  {_header(home=True, title="Files")}

  <main class="vt-tool vt-files" id="vt-files">
    <div class="vt-glass vt-panel-card vt-files-card">
      <div class="vt-files-head">
        <nav class="vt-crumbs" id="vt-crumbs" aria-label="Folder"></nav>
        <p class="vt-files-where" id="vt-files-where">&nbsp;</p>
      </div>

      <div class="vt-files-controls">
        <div class="vt-seg" id="vt-files-filter" role="group" aria-label="Show only"></div>
        <div class="vt-files-right">
          <label class="vt-files-sort">
            <span>Sort</span>
            <select id="vt-files-sort" class="vt-option-select"></select>
          </label>
          <button type="button" class="vt-icon-button" id="vt-files-order"
                  title="Reverse the sort order" aria-label="Reverse the sort order"></button>
          <div class="vt-seg" id="vt-files-view" role="group" aria-label="View as"></div>
          <button type="button" class="vt-icon-button" id="vt-files-columns-toggle"
                  aria-haspopup="true" aria-expanded="false"
                  title="Choose columns" aria-label="Choose columns"></button>
        </div>
      </div>

      <div class="vt-columns-menu" id="vt-files-columns" hidden>
        <p class="vt-columns-title">Columns</p>
        <div id="vt-files-columns-list"></div>
        <p class="vt-hint">Shown in Details, and each one can be sorted by.</p>
      </div>

      <div class="vt-files-body" id="vt-files-body" aria-live="polite"></div>
      <p class="vt-status" id="vt-files-status" role="status" aria-live="polite"></p>
    </div>
  </main>
</div>

<div class="vt-lightbox" id="vt-lightbox" hidden role="dialog" aria-label="Picture">
  <div class="vt-lightbox-bar">
    <span class="vt-lightbox-name" id="vt-lightbox-name"></span>
    <div class="vt-lightbox-actions">
      <a class="vt-button vt-lightbox-download" id="vt-lightbox-download"
         href="#" download>Download</a>
      <button type="button" class="vt-icon-button" id="vt-lightbox-close"
              title="Close (Esc)" aria-label="Close">&times;</button>
    </div>
  </div>
  <button type="button" class="vt-lightbox-step" id="vt-lightbox-prev"
          aria-label="Previous picture">&lsaquo;</button>
  <img class="vt-lightbox-image" id="vt-lightbox-image" alt="" />
  <button type="button" class="vt-lightbox-step vt-lightbox-next" id="vt-lightbox-next"
          aria-label="Next picture">&rsaquo;</button>
</div>

<section class="vt-player-shell" id="vt-player-shell" hidden>
  <div class="vt-player-bar">
    <button type="button" class="vt-button" id="vt-player-back">&larr; Back to files</button>
    <span class="vt-player-name" id="vt-player-name"></span>
  </div>
  {PLAYER_MARKUP}
</section>
"""
    return render_page(
        f"Files — {PRODUCT_NAME}", "files", body, version,
        extra_styles=("player.css", "files.css"),
        extra_scripts=("player.js", "files.js"),
    )


def _header(home=True, title=""):
    """One header, one place for Home/Back, on every tool.

    Same position and same touch size everywhere, so the way out never has to be
    hunted for.
    """
    left = (
        '<a class="vt-home-link" href="/" aria-label="Back to Home">'
        '<span class="vt-home-icon" aria-hidden="true"></span><span>Home</span></a>'
        if home else '<span class="vt-home-link vt-home-link-inert"></span>'
    )
    heading = f'<span class="vt-header-title">{html.escape(title)}</span>' if title else ""
    return f"""
<header class="vt-header">
  {left}
  {heading}
  <button type="button" class="vt-signout" id="vt-signout">Sign out</button>
</header>
"""
