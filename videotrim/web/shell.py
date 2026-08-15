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
</head>
<body class="vt-body" data-view="{view}">
{body}
<script src="/vt/static/{script}?v={version}" defer></script>
</body>
</html>
"""


def render_page(title, view, body, version, stylesheet="shell.css", script="shell.js"):
    return _PAGE.format(
        title=html.escape(title),
        view=html.escape(view),
        body=body,
        version=html.escape(str(version)),
        stylesheet=stylesheet,
        script=script,
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
