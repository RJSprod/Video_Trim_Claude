#!/usr/bin/env python3
"""Video Trim WebUI — the browser front end.

Normally started for you by the one-click launcher (``start_windows.bat``,
``start_linux.sh``, ``start_macos.sh``), which builds the ``venv`` folder beside
this file and makes sure a username and password exist first. To run it by hand,
activate that venv and:

    python webui.py                     # port 7862, on every interface
    python webui.py --local-only        # this machine only
    python webui.py --listen-port 7900  # somewhere other than 7862

Serving the whole network is the default so a phone or laptop can open the app
and send a video. Everyone must sign in — there is no unauthenticated surface
except the login page itself — and a new remote address cannot cause the host to
write anything until the host allows it, from the host.

Where saves go is the host's decision and has no default. Set it in Settings, or
pass ``--output-dir`` once. It must already exist; this app never creates a
folder outside its own directory, and never falls back to the Desktop.

What this does not protect against, stated plainly: over plain HTTP on a LAN the
session cookie travels in the clear, and anyone able to capture traffic on the
network segment can take over a session. Put it behind HTTPS if that matters.
"""

import argparse
import errno
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parent))

# The port this app claims. Kept as a constant rather than an ad-hoc default so
# there is exactly one place that says 7862.
DEFAULT_PORT = 7862

# Serve on every interface by default, so a phone or laptop elsewhere on the
# network can open the app without anyone having to pass a flag. Unlike before,
# that is no longer an open door: every route requires a session, and a new
# address is write-denied until the host says otherwise.
DEFAULT_HOST = "0.0.0.0"
LOOPBACK_HOST = "127.0.0.1"

CMD_FLAGS_FILE = Path(__file__).resolve().parent / "CMD_FLAGS.txt"

OUTPUT_ENV = "VIDEOTRIM_OUTPUT_DIR"


def _missing_dependency(exc):
    print(
        f"Video Trim WebUI could not start: {exc}\n\n"
        "It needs Gradio, which lives in the venv the one-click installer builds.\n"
        "Either run the launcher for your platform:\n\n"
        "    start_windows.bat   /   ./start_linux.sh   /   ./start_macos.sh\n\n"
        "…or install into the current environment with:\n\n"
        "    pip install -r requirements-webui.txt\n",
        file=sys.stderr,
    )
    return 1


def read_cmd_flags():
    """Extra flags from CMD_FLAGS.txt, so the launcher scripts stay untouched.

    Never a place for a password: this file sits in the install directory in
    plain text, and the credential path deliberately does not read from it.
    """
    if not CMD_FLAGS_FILE.is_file():
        return []
    flags = []
    for line in CMD_FLAGS_FILE.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            flags += line.split()
    return flags


def build_parser():
    parser = argparse.ArgumentParser(
        prog="webui.py",
        description="Video Trim in the browser: A-B looping, clip export and stills.",
    )
    parser.add_argument(
        "--listen-port", type=int, default=DEFAULT_PORT,
        help=f"port to serve on (default {DEFAULT_PORT})",
    )
    parser.add_argument(
        "--local-only", action="store_true",
        help=(
            "bind 127.0.0.1 only, so nothing outside this machine can reach the "
            "app. The default is to serve the whole network."
        ),
    )
    parser.add_argument(
        # Kept because it is what CMD_FLAGS.txt and older notes tell people to
        # use; serving the network is now the default, so it does nothing.
        "--listen", action="store_true", help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--listen-host", default=None,
        help=f"exact interface to bind (default {DEFAULT_HOST} — every interface)",
    )
    parser.add_argument(
        "--share", action="store_true",
        help=(
            "also expose a temporary public gradio.live URL. Host Settings are "
            "hidden while a tunnel is up, because tunnelled traffic cannot be "
            "told apart from the host's own."
        ),
    )
    parser.add_argument(
        "--no-browser", action="store_true",
        help="do not open a browser window on start",
    )
    parser.add_argument(
        "--allow-remote-files", action="store_true",
        help=(
            "let signed-in visitors from other machines open and browse paths on "
            "this one. Grants reading only — never Settings, credentials, the "
            "save location, or IP permissions."
        ),
    )
    parser.add_argument(
        "--output-dir", default=None,
        help=(
            "set the save folder. It must already exist and must sit outside the "
            "Video Trim installation. Stored, so this is only needed once."
        ),
    )
    parser.add_argument(
        "--proxy-height", type=int, default=720,
        help="height of the transcoded preview built for codecs a browser cannot play",
    )
    parser.add_argument(
        "--any-port", action="store_true",
        help="fall back to a free port instead of failing when the port is taken",
    )
    parser.add_argument("video", nargs="?", help="a video to open straight away")
    return parser


def port_is_free(host, port):
    """True if we can actually bind ``port``.

    Checked up front so a busy port is a clear message rather than the server
    quietly serving on 7863 — the whole point is that 7862 is the address the
    user has been told to use.
    """
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((host, port))
        except OSError as exc:
            if exc.errno in (errno.EADDRINUSE, errno.EACCES, getattr(errno, "WSAEADDRINUSE", -1)):
                return False
            raise
    return True


def pick_free_port(host, first):
    for port in range(first, first + 40):
        if port_is_free(host, port):
            return port
    raise SystemExit(f"No free port found between {first} and {first + 40}.")


def busy_port_message(host, port):
    hint = (
        'netstat -ano | findstr :{port}'
        if sys.platform == "win32"
        else 'lsof -i :{port}   (or: ss -ltnp | grep :{port})'
    ).format(port=port)
    return (
        f"Port {port} on {host} is already in use, so Video Trim did not start.\n\n"
        f"That port is reserved for this app. Find what has it with:\n\n"
        f"    {hint}\n\n"
        "Then stop that process and try again — or start on a different port with\n"
        f"    python webui.py --listen-port <other>\n"
        "or let it pick the next free one with --any-port."
    )


def lan_addresses():
    """This machine's own routable addresses, best effort.

    Printed in the banner because that is the URL you type on the phone, and
    hostnames often do not resolve from other devices on a home network.
    """
    found = []

    # The address that would be used to reach the outside world — on a normal
    # home network, the one other devices can also reach. No packets are sent.
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("192.0.2.1", 9))  # reserved, unroutable documentation address
        found.append(probe.getsockname()[0])
    except OSError:
        pass
    finally:
        probe.close()

    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            found.append(info[4][0])
    except (OSError, socket.gaierror):
        pass

    ordered = []
    for address in found:
        if address and not address.startswith("127.") and address not in ordered:
            ordered.append(address)
    return ordered


def open_browser_later(url, delay=1.5):
    def opener():
        time.sleep(delay)
        try:
            webbrowser.open(url)
        except Exception:
            pass

    threading.Thread(target=opener, name="vt-open-browser", daemon=True).start()


def start_tunnel(host, port):
    """Best effort gradio.live tunnel, since we run uvicorn ourselves."""
    try:
        import secrets

        from gradio import networking
    except Exception:
        return None
    try:
        return networking.setup_tunnel(host, port, secrets.token_urlsafe(32), None, None)
    except TypeError:
        try:
            return networking.setup_tunnel(host, port, secrets.token_urlsafe(32), None)
        except Exception:
            return None
    except Exception:
        return None


def preflight(store):
    """Refuse to open a listener without credentials and a working verifier.

    ``data/`` survives ``--recreate`` while the venv does not, so the database
    can outlive the hashing library. Starting anyway would mean either a crash at
    the first login or, worse, somebody "fixing" it with a weaker hash.
    """
    import os

    from videotrim.security.auth import HASHER_MISSING, hasher_available

    if not hasher_available():
        print(HASHER_MISSING, file=sys.stderr)
        return False

    if store.user_count() == 0:
        print(
            "Video Trim has no username and password yet, so it will not start.\n\n"
            "Run the launcher to set one:\n\n"
            "    python one_click.py\n",
            file=sys.stderr,
        )
        return False

    # Owner-only, re-applied on every start rather than only at creation.
    if os.name == "posix":
        try:
            os.chmod(store.path.parent, 0o700)
            os.chmod(store.path, 0o600)
        except OSError:
            pass
    return True


def apply_output_dir(settings, raw):
    """Store an explicit save folder. Existing directory, no mkdir, no fallback."""
    from videotrim.security.fs_boundary import OutputRootError

    try:
        chosen = settings.set_save_location(raw)
    except OutputRootError as exc:
        print(f"Video Trim did not start: {exc}", file=sys.stderr)
        return None
    return chosen


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    args = build_parser().parse_args(read_cmd_flags() + argv)

    try:
        import os

        import uvicorn

        from videotrim.config.settings import SettingsService
        from videotrim.config.store import Store
        from videotrim.security.auth import TRANSPORT_WARNING
        from videotrim.web.server import create_app
    except ImportError as exc:
        return _missing_dependency(exc)

    store = Store()
    if not preflight(store):
        return 1

    settings = SettingsService(store)
    raw_output = args.output_dir or os.environ.get(OUTPUT_ENV, "").strip()
    if raw_output and apply_output_dir(settings, raw_output) is None:
        return 1

    host = args.listen_host or (LOOPBACK_HOST if args.local_only else DEFAULT_HOST)
    port = args.listen_port

    if not port_is_free(host, port):
        if not args.any_port:
            print(busy_port_message(host, port), file=sys.stderr)
            return 1
        port = pick_free_port(host, port + 1)
        print(f"Port {args.listen_port} was busy; using {port} instead.")

    # A tunnel makes every request arrive from the tunnel client, so the host
    # cannot be told apart from anyone else. Settings fail closed rather than
    # risk a false "this is the host" classification.
    app = create_app(
        allow_remote_files=args.allow_remote_files,
        proxy_height=args.proxy_height,
        store=store,
        tunnel_active=args.share,
    )

    shown_host = LOOPBACK_HOST if host in ("0.0.0.0", "::") else host
    url = f"http://{shown_host}:{port}"
    state = app.state.video_trim
    everywhere = host in ("0.0.0.0", "::")

    print("\n  Video Trim")
    print(f"  ├─ on this machine   {url}")
    if everywhere:
        for address in lan_addresses():
            print(f"  ├─ from elsewhere    http://{address}:{port}")
        print("  ├─                   …everyone must sign in, and a new device "
              "cannot save anything")
        print("  ├─                   until you allow its address in Settings.")
        print("  ├─                   Use --local-only to keep it to this machine.")
    # The banner is host console output, so a real path here is fine. Remote
    # responses never carry one.
    saved_to = settings.raw_save_location or "NOT SET — choose one in Settings"
    print(f"  ├─ saving to         {saved_to}")
    print(f"  ├─ ffmpeg            {state.ffmpeg or 'NOT FOUND — exports disabled'}")
    print(f"  └─ note              {TRANSPORT_WARNING}\n")

    if args.share:
        public = start_tunnel(shown_host, port)
        if public:
            print(f"  public URL     {public}")
            print("  Host Settings are hidden while a tunnel is up.\n")
        else:
            print("  --share could not open a tunnel; serving locally only.\n")

    if not args.no_browser:
        target = f"{url}/?__theme=dark"
        if args.video:
            # Mirrors `python app.py <file>`: open straight into a video.
            target = (f"{url}/tools/video-trim?__theme=dark&open="
                      + quote(str(Path(args.video).expanduser()), safe=""))
        open_browser_later(target)

    try:
        uvicorn.run(app, host=host, port=port, log_level="warning", access_log=False)
    except KeyboardInterrupt:
        pass
    finally:
        state.jobs.cancel_all()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
