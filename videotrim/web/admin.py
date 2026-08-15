"""Host-only Settings.

Every route here demands the strict host-local predicate, which is a different
question from "may this client browse host paths". ``--allow-remote-files``
widens the second and never the first, so a remote session gets 403 from the
server whether or not the UI drew the card.
"""

import string
import sys
from pathlib import Path

from fastapi import HTTPException, Request

from ..config.store import IP_HISTORY_CAP
from ..paths import desktop_dir
from ..security.auth import AuthError, validate_credentials
from ..security.fs_boundary import OutputRootError, validate_output_root
from ..security.network import normalize_ip
from .parsing import read_json


def _require_host(state, request):
    """The one gate. Called first in every handler below, no exceptions."""
    if not state.host_guard.is_host_request(request):
        raise HTTPException(
            status_code=403,
            detail="Settings can only be changed from the machine running Video Trim.",
        )
    return state.session_for(request)


def register_admin_routes(app, state):
    @app.get("/vt/api/settings")
    def read_settings(request: Request):
        session = _require_host(state, request)
        return {
            "username": session.username if session else "",
            "save_location": state.settings.raw_save_location,
            "save_label": state.settings.save_label,
            "save_writable": _writable(state.settings.raw_save_location),
            "ip_history_cap": IP_HISTORY_CAP,
            "ips": [_ip_row(row) for row in state.store.list_ips()],
            "notices": [_notice(row) for row in state.journal.survivors()],
        }

    # --- account -------------------------------------------------------------
    @app.post("/vt/api/settings/account")
    async def change_account(request: Request):
        """Rotate the login. Revokes every other session and its media tokens."""
        session = _require_host(state, request)
        body = await read_json(request)
        current = str(body.get("current_password") or "")
        username = str(body.get("username") or "").strip()
        password = str(body.get("password") or "")

        try:
            validate_credentials(username, password)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        try:
            state.auth.verify(session.username, current)
        except AuthError as exc:
            raise HTTPException(status_code=403, detail="That current password is wrong.") from exc

        state.auth.rotate_credentials(username, password, keep_token=session.token)
        return {"status": "ok", "username": username,
                "message": "Account updated. Other devices have been signed out."}

    # --- save location -------------------------------------------------------
    @app.post("/vt/api/settings/save-location")
    async def set_save_location(request: Request):
        """Point saves at an existing folder. Never creates one, never falls back."""
        _require_host(state, request)
        body = await read_json(request)
        raw = str(body.get("path") or "").strip().strip('"').strip("'")
        if not raw:
            raise HTTPException(status_code=400, detail="Choose a folder.")
        try:
            chosen = state.settings.set_save_location(raw)
        except OutputRootError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "status": "ok",
            "save_location": str(chosen),
            "save_writable": _writable(chosen),
            "message": f"Saves will be created in {chosen}.",
        }

    @app.post("/vt/api/settings/save-label")
    async def set_save_label(request: Request):
        """The name other devices see instead of the path."""
        _require_host(state, request)
        body = await read_json(request)
        state.settings.set_save_label(str(body.get("label") or ""))
        return {"status": "ok", "save_label": state.settings.save_label}

    @app.get("/vt/api/settings/browse")
    def browse_folders(request: Request, dir: str = ""):
        """Folder names only, for picking a save location. No write side effects."""
        _require_host(state, request)
        current = Path(dir).expanduser() if dir else Path.home()
        try:
            current = current.resolve()
        except OSError:
            current = Path.home()
        if not current.is_dir():
            current = Path.home()

        folders = []
        try:
            for item in sorted(current.iterdir(), key=lambda p: p.name.lower()):
                if item.name.startswith("."):
                    continue
                try:
                    if item.is_dir():
                        folders.append({"name": item.name, "path": str(item)})
                except OSError:
                    continue
        except PermissionError as exc:
            raise HTTPException(status_code=403,
                                detail=f"{current} is not readable.") from exc

        return {
            "dir": str(current),
            "parent": str(current.parent) if current.parent != current else "",
            "folders": folders,
            "shortcuts": _shortcuts(),
            "eligible": _eligible(current),
        }

    # --- IP access -----------------------------------------------------------
    @app.post("/vt/api/settings/ip")
    async def set_ip_permission(request: Request):
        """Flip one address. Persists immediately; the next check reflects it."""
        _require_host(state, request)
        body = await read_json(request)
        address = normalize_ip(body.get("ip"))
        if not address:
            raise HTTPException(status_code=400, detail="Which address?")

        if "write_allowed" in body:
            allowed = bool(body.get("write_allowed"))
            state.store.set_write_allowed(address, allowed)
            if not allowed:
                # Revocation reaches work that is still entirely internal. A
                # commit already past publication is finished and stays that way.
                state.jobs.cancel_for_ip(address)
        if "label" in body:
            state.store.set_ip_label(address, body.get("label"))
        if body.get("dismiss_request"):
            state.store.clear_access_request(address)

        row = state.store.ip_row(address)
        return {"status": "ok", "ip": _ip_row(row) if row else None}

    # --- maintenance ---------------------------------------------------------
    @app.post("/vt/api/settings/notice/{notice_id}/acknowledge")
    def acknowledge_notice(notice_id: int, request: Request):
        """Clear a notice. Removes the row and touches nothing on disk.

        There is deliberately no delete or repair action: a later run cannot
        prove it created those files, so it must never act on them.
        """
        _require_host(state, request)
        state.journal.acknowledge(notice_id)
        return {"status": "ok"}


# --- helpers -----------------------------------------------------------------
def _writable(path):
    if not path:
        return False
    from ..security.fs_boundary import output_root_is_writable
    return output_root_is_writable(path)


def _eligible(path):
    """Whether this folder could be chosen, and why not when it cannot."""
    try:
        validate_output_root(path)
        return {"ok": True, "reason": ""}
    except OutputRootError as exc:
        return {"ok": False, "reason": str(exc)}


def _shortcuts():
    places = [{"name": "Home", "path": str(Path.home())}]
    desktop = desktop_dir()
    if desktop and desktop.is_dir():
        places.append({"name": "Desktop", "path": str(desktop)})
    for label in ("Pictures", "Videos", "Movies", "Downloads", "Documents"):
        candidate = Path.home() / label
        if candidate.is_dir():
            places.append({"name": label, "path": str(candidate)})
    if sys.platform == "win32":
        for letter in string.ascii_uppercase:
            drive = Path(f"{letter}:\\")
            if drive.exists():
                places.append({"name": f"{letter}:", "path": str(drive)})
    return places


def _ip_row(row):
    return {
        "ip": row["ip"],
        "label": row["label"],
        "first_seen": row["first_seen"],
        "last_seen": row["last_seen"],
        "failed_logins": row["failed_logins"],
        "successful_logins": row["successful_logins"],
        "last_success": row["last_success"],
        "write_allowed": bool(row["write_allowed"]),
        "access_requested_at": row["access_requested_at"],
        "access_requested_by": row["access_requested_by"],
    }


def _notice(row):
    return {
        "id": row["id"],
        "output_dir": row["output_dir"],
        "basename": row["basename"],
        "created_at": row["created_at"],
        "job_id": row["job_id"],
    }
