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

Serving the network means HTTPS, with nothing to set up: Video Trim makes its
own certificate in ``data/tls/`` and reuses it (see videotrim/security/tls.py).
It is self-signed, so each device's browser warns once before connecting. Plain
HTTP happens only when asked for — ``--http`` — or when ``--local-only`` keeps
the traffic on this machine anyway. If HTTPS cannot be set up the app does not
start; it never quietly falls back to HTTP.
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

# Gradio's tunnel runs frpc as an HTTP proxy that opens plain TCP to the local
# port, in every Gradio release this app supports. Pointed at HTTPS it still
# prints a public link — frpc never touches the local port until a request
# arrives — and then every request through that link fails. So the combination
# is refused before anything starts, rather than quietly serving the network
# over HTTP to make the tunnel work.
SHARE_NEEDS_HTTP = """\
Video Trim did not start: --share cannot reach an HTTPS server.

Gradio's share tunnel connects to this app over plain HTTP, so with HTTPS on
it would print a public link that fails on every request. Choose one:

    python webui.py --share --local-only
        serve this machine only; the public link itself is still HTTPS

    python webui.py --share --http
        also serve your whole network, over unencrypted HTTP
"""

BROWSER_TRUST = (
    "Video Trim generated its own certificate to encrypt this",
    "connection. Browsers do not automatically trust locally",
    "generated certificates, so each device — this one included —",
    "may show a warning the first time. Video Trim does not modify",
    "any device's trust store.",
)


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
            "app. The default is to serve the whole network. Served over HTTP, "
            "since loopback traffic never leaves this machine."
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
            "told apart from the host's own. The tunnel can only reach plain "
            "HTTP, so combine it with --local-only (or --http)."
        ),
    )
    parser.add_argument(
        "--http", action="store_true",
        help=(
            "serve plain, unencrypted HTTP instead of HTTPS. Anyone who can "
            "capture traffic on your network can then read the session cookie "
            "and the media."
        ),
    )
    parser.add_argument(
        "--tls-keyfile", metavar="PATH", default=None,
        help=(
            "your own private key (PEM, without a passphrase), instead of the "
            "certificate Video Trim makes. Needs --tls-certfile. Only ever read."
        ),
    )
    parser.add_argument(
        "--tls-certfile", metavar="PATH", default=None,
        help="your own certificate (PEM, leaf first) for --tls-keyfile. Only ever read.",
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


def check_transport_flags(parser, args):
    """Refuse flags that contradict each other, before anything is touched."""
    supplied = [
        flag for flag, value in (("--tls-keyfile", args.tls_keyfile),
                                 ("--tls-certfile", args.tls_certfile))
        if value
    ]
    if args.http and supplied:
        parser.error(
            f"--http serves plain HTTP, so it cannot be combined with {' or '.join(supplied)}."
        )
    if len(supplied) == 1:
        parser.error("--tls-keyfile and --tls-certfile have to be given together.")


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


def lan_addresses(primary=None):
    """This machine's own routable addresses, best effort.

    Printed in the banner because that is the URL you type on the phone, and
    hostnames often do not resolve from other devices on a home network. The
    route-probe address leads, because it is the one the certificate always
    covers. ``primary`` is that address when the caller already has it.
    """
    from videotrim.security import network

    found = [network.primary_address() if primary is None else primary]
    found += network.hostname_addresses(socket.AF_INET)

    ordered = []
    for address in found:
        if address and not address.startswith("127.") and address not in ordered:
            ordered.append(address)
    return ordered


def url_for(scheme, host, port):
    """A URL a browser accepts, bracketing an IPv6 literal."""
    text = str(host)
    if ":" in text and not text.startswith("["):
        text = f"[{text}]"
    return f"{scheme}://{text}:{port}"


def transport_rows(transport, advertised, share=False):
    """The banner's account of how this launch serves, as (label, lines) rows.

    ``advertised`` is every URL the banner prints, so a supplied certificate
    that does not name one of them is said so here rather than discovered on a
    phone.
    """
    from videotrim.security import tls

    material = transport.material
    if transport.mode == tls.MANAGED:
        where = tls.display_path(material.cert_path)
        if material.regenerated:
            certificate = [f"{where} (new)", f"…made this launch: {material.reason}"]
            if material.reason != tls.FIRST_CERTIFICATE:
                certificate.append("…devices that accepted the previous one will be asked again")
        else:
            certificate = [f"{where} (reused)"]
        rows = [
            ("transport", ["HTTPS (managed self-signed certificate)"]),
            ("certificate", certificate),
        ]
        rows.append(("fingerprint", _fingerprint_lines(material.fingerprint)))
        rows.append(("browser trust", list(BROWSER_TRUST)))
    elif transport.mode == tls.SUPPLIED:
        rows = [
            ("transport", ["HTTPS (supplied certificate)"]),
            ("certificate", [str(material.cert_path)]),
        ]
        if material.fingerprint:
            rows.append(("fingerprint", _fingerprint_lines(material.fingerprint)))
        if material.identities:
            for url, host in advertised:
                if not material.covers(host):
                    rows.append(("warning", [f"the certificate does not name {host}, so",
                                             f"browsers will warn at {url}"]))
    elif transport.mode == tls.PLAINTEXT:
        rows = [
            ("transport", ["HTTP — NOT encrypted (--http)"]),
            ("warning", ["session cookies and media can be observed on the network"]),
        ]
    elif share:
        rows = [("transport", ["HTTP on this machine only; a --share link is HTTPS"])]
    else:
        rows = [("transport", ["HTTP on this machine only — nothing crosses the network"])]
    for warning in material.warnings if material is not None else ():
        rows.append(("warning", [warning]))
    return rows


def banner_lines(transport, shown_host, port, lan, saved_to, ffmpeg, share=False):
    """The startup banner. ``lan`` is None unless every interface is served."""
    from videotrim.security import tls

    url = url_for(transport.scheme, shown_host, port)
    rows = [("on this machine", [url])]
    advertised = [(url, shown_host)]
    if lan is not None:
        uncovered = []
        for address in lan:
            if transport.mode == tls.MANAGED and not transport.material.covers(address):
                # Never printed as a URL: a certificate that does not name an
                # address is not advertised as serving it.
                if tls.nameable(address):
                    uncovered.append(address)
                continue
            lan_url = url_for(transport.scheme, address, port)
            rows.append(("from elsewhere", [lan_url]))
            advertised.append((lan_url, address))
        if uncovered:
            rows.append(("uncovered", [
                ", ".join(uncovered),
                "…also this machine, but not named in its certificate. To add",
                "them, delete data/tls/ and restart; each device will then be",
                "asked to accept the new certificate.",
            ]))
        rows.append(("", [
            "…everyone must sign in, and a new device cannot save anything",
            "until you allow its address in Settings.",
            "Use --local-only to keep it to this machine.",
        ]))
    # The banner is host console output, so a real path here is fine. Remote
    # responses never carry one.
    rows.append(("saving to", [saved_to]))
    rows.append(("ffmpeg", [ffmpeg]))
    rows.extend(transport_rows(transport, advertised, share=share))
    return _tree(rows)


def _fingerprint_lines(fingerprint):
    """SHA-256 fingerprint split in two, so it fits a console and can be compared."""
    if len(fingerprint) == 95:
        return [f"SHA-256 {fingerprint[:47]}", f"        {fingerprint[48:]}"]
    return [f"SHA-256 {fingerprint}"]


def _tree(rows):
    lines = ["", "  Video Trim"]
    for index, (label, texts) in enumerate(rows):
        last = index == len(rows) - 1
        for position, text in enumerate(texts):
            if position == 0:
                branch = "└─" if last else "├─"
                lines.append(f"  {branch} {label:<17} {text}")
            else:
                branch = "  " if last else "├─"
                lines.append(f"  {branch} {'':<17} {text}")
    lines.append("")
    return lines


def tls_failure_message(exc, supplied):
    if supplied:
        remedy = ("Fix the files passed to --tls-keyfile and --tls-certfile and try "
                  "again. Video Trim only reads them and never changes them.")
    else:
        remedy = ("Fix that and try again. Video Trim keeps its certificate in "
                  "data/tls/; deleting that folder makes it generate a new one.")
    return (
        "Video Trim did not start because HTTPS setup failed:\n\n"
        f"    {exc}\n\n"
        f"{remedy}\n"
        "If you intentionally accept unencrypted traffic on your network, start "
        "with --http.\n"
    )


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
    parser = build_parser()
    args = parser.parse_args(read_cmd_flags() + argv)
    check_transport_flags(parser, args)

    try:
        import os
        import ssl

        import uvicorn

        from videotrim.config.settings import SettingsService
        from videotrim.config.store import Store
        from videotrim.security import network, tls
        from videotrim.web.server import create_app
    except ImportError as exc:
        return _missing_dependency(exc)

    host = args.listen_host or (LOOPBACK_HOST if args.local_only else DEFAULT_HOST)
    supplied = bool(args.tls_keyfile)
    if args.share and tls.expects_tls(host, plaintext=args.http, supplied=supplied):
        print(SHARE_NEEDS_HTTP, file=sys.stderr)
        return 1

    store = Store()
    if not preflight(store):
        return 1

    settings = SettingsService(store)
    raw_output = args.output_dir or os.environ.get(OUTPUT_ENV, "").strip()
    if raw_output and apply_output_dir(settings, raw_output) is None:
        return 1

    port = args.listen_port

    if not port_is_free(host, port):
        if not args.any_port:
            print(busy_port_message(host, port), file=sys.stderr)
            return 1
        port = pick_free_port(host, port + 1)
        print(f"Port {args.listen_port} was busy; using {port} instead.")

    # Discovered once, so the certificate and the banner agree on what "this
    # machine's address" is.
    everywhere = tls.is_wildcard(host)
    primary = network.primary_address() if everywhere else ""
    lan = lan_addresses(primary) if everywhere else None

    # Before anything is announced: if HTTPS cannot be prepared the launch
    # stops here, and never continues over HTTP as if nothing happened.
    try:
        transport = tls.resolve_transport(
            host,
            plaintext=args.http,
            keyfile=args.tls_keyfile,
            certfile=args.tls_certfile,
            primary=primary,
            addresses=lan or [],
        )
    except tls.TLSError as exc:
        print(tls_failure_message(exc, supplied), file=sys.stderr)
        return 1

    # A tunnel makes every request arrive from the tunnel client, so the host
    # cannot be told apart from anyone else. Settings fail closed rather than
    # risk a false "this is the host" classification.
    app = create_app(
        allow_remote_files=args.allow_remote_files,
        proxy_height=args.proxy_height,
        store=store,
        tunnel_active=args.share,
        transport=transport,
    )
    state = app.state.video_trim

    config = uvicorn.Config(app, host=host, port=port, log_level="warning",
                            access_log=False, **transport.uvicorn_options())
    try:
        # Loaded now rather than inside run(): under the certificate lock, so
        # another launch cannot swap the pair between the check and this read,
        # and before the banner, so a refusal is a clear message, not a success
        # banner followed by a traceback.
        with tls.holding(transport):
            config.load()
    except (tls.TLSError, ssl.SSLError, OSError) as exc:
        print(tls_failure_message(exc, supplied), file=sys.stderr)
        return 1

    shown_host = LOOPBACK_HOST if everywhere else host
    url = url_for(transport.scheme, shown_host, port)
    saved_to = settings.raw_save_location or "NOT SET — choose one in Settings"
    ffmpeg = state.ffmpeg or "NOT FOUND — exports disabled"
    for line in banner_lines(transport, shown_host, port, lan, saved_to, ffmpeg,
                             share=args.share):
        print(line)

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

    server = uvicorn.Server(config)
    try:
        server.run()
    except KeyboardInterrupt:
        pass
    finally:
        state.jobs.cancel_all()
    if not server.started:
        # Uvicorn has already said why (usually the port); do not report success.
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
