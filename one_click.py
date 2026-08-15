#!/usr/bin/env python3
"""One-click installer and launcher for the Video Trim WebUI.

Modelled on Oobabooga's text-generation-webui launcher, with one deliberate
difference: dependencies go into a plain ``venv`` folder beside this file rather
than a Miniconda environment. Nothing is installed system-wide and nothing is
written outside this directory, so deleting the folder uninstalls everything.

    venv/                     every dependency lives here
    data/                     credentials, settings and IP history (survives --recreate)
    cache/                    uploads, staging and preview transcodes
    CMD_FLAGS.txt             flags added to every launch

Run it through the launcher for your platform rather than directly:

    start_windows.bat     start_linux.sh     start_macos.sh

Useful flags (everything else is passed through to webui.py):

    --update           reinstall requirements into the existing venv
    --recreate         delete the venv and build it again from scratch
    --desktop          also install PySide6 so the Qt app (`python app.py`) works
    --install          set up the venv and exit without launching
    --verbose          show pip's output instead of a spinner
    --change-auth      set a new username and password, then exit

Credentials are created before the network listener ever starts, and live in
``data/app.db`` rather than in the venv — so ``--recreate`` keeps them. That does
mean "credential database present, hashing library absent" is a state you can
reach, which is why the launcher imports the Argon2id library and refuses to
start rather than quietly falling back to something weaker.

Never put a password in CMD_FLAGS.txt.
"""

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import venv
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV_DIR = ROOT / "venv"
STATE_FILE = VENV_DIR / ".video-trim-install.json"

WEBUI_REQUIREMENTS = ROOT / "requirements-webui.txt"
DESKTOP_REQUIREMENTS = ROOT / "requirements.txt"

MIN_PYTHON = (3, 9)

# Our own flags; anything else on the command line belongs to webui.py.
OURS = {"--update", "--recreate", "--desktop", "--install", "--verbose",
        "--change-auth"}

# Flags that take a value, so the value is not passed through to webui.py either.
OURS_WITH_VALUE = {"--auth-user", "--auth-password", "--auth-password-env"}

# The credential helper runs inside the venv, because that is where the Argon2id
# library lives. Kept as source rather than a module so a partly-built install
# cannot half-import it.
_CREDENTIAL_SCRIPT = r'''
import getpass
import os
import sys

sys.path.insert(0, os.getcwd())

from videotrim.config.store import Store
from videotrim.security.auth import (
    AuthService,
    HasherUnavailable,
    MIN_PASSWORD_LENGTH,
    hasher,
)

mode = sys.argv[1]            # "ensure" or "rotate"
cli_user = sys.argv[2] if len(sys.argv) > 2 else ""
cli_password = sys.argv[3] if len(sys.argv) > 3 else ""

try:
    hasher()
except HasherUnavailable as exc:
    print(str(exc), file=sys.stderr)
    raise SystemExit(2)

store = Store()
auth = AuthService(store)

if mode == "ensure" and auth.has_credentials():
    raise SystemExit(0)

def ask():
    print()
    print("Video Trim needs a username and password before it can serve anything.")
    print("These are stored in data/app.db as an Argon2id hash, never in plain text,")
    print("and they survive updates and --recreate.")
    print()
    while True:
        username = input("  Username: ").strip()
        if not username:
            print("  A username is required.")
            continue
        password = getpass.getpass("  Password: ")
        if len(password) < MIN_PASSWORD_LENGTH:
            print(f"  Use at least {MIN_PASSWORD_LENGTH} characters.")
            continue
        again = getpass.getpass("  Password again: ")
        if password != again:
            print("  Those did not match. Try again.")
            continue
        return username, password

if cli_user and cli_password:
    username, password = cli_user, cli_password
elif sys.stdin.isatty():
    username, password = ask()
else:
    print(
        "No credentials exist and this is not an interactive terminal.\n"
        "Run the launcher from a console, or pass --auth-user with either\n"
        "--auth-password-env <NAME> or --auth-password <value>.",
        file=sys.stderr,
    )
    raise SystemExit(2)

try:
    if auth.has_credentials():
        auth.rotate_credentials(username, password)
        print("\nCredentials updated. Every other signed-in device has been signed out.")
    else:
        auth.create_credentials(username, password)
        print("\nCredentials created.")
except ValueError as exc:
    print(f"\n{exc}", file=sys.stderr)
    raise SystemExit(2)
'''


# --- console ------------------------------------------------------------------
def say(message=""):
    print(message, flush=True)


def banner(message):
    say()
    say(f"*** {message} ***")


def die(message):
    say()
    say(f"ERROR: {message}")
    sys.exit(1)


# --- venv paths ---------------------------------------------------------------
def venv_python(base=VENV_DIR):
    """The interpreter inside the venv, however this platform lays it out."""
    candidates = [base / "bin" / "python", base / "Scripts" / "python.exe"]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return candidates[1] if os.name == "nt" else candidates[0]


def venv_is_usable():
    python = venv_python()
    if not python.is_file():
        return False
    try:
        subprocess.run([str(python), "-c", "import sys"], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except (OSError, subprocess.CalledProcessError):
        return False


# --- checks -------------------------------------------------------------------
def check_environment():
    if sys.version_info[:2] < MIN_PYTHON:
        die(
            f"Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]} or newer is required, but this "
            f"is {platform.python_version()}.\nInstall a newer Python and run the "
            "launcher again."
        )

    # A path with quotes or unusual characters breaks pip and the shell wrappers.
    bad = [ch for ch in ('"', "'", "#", "%") if ch in str(ROOT)]
    if bad:
        die(
            f"The folder path contains {' '.join(bad)}, which breaks the installer.\n"
            f"Move the folder somewhere simpler and try again:\n    {ROOT}"
        )
    if " " in str(ROOT):
        say(
            "Note: this folder path contains spaces. That is handled here, but if "
            "you hit odd tool errors later, a path without spaces is safer."
        )

    if not WEBUI_REQUIREMENTS.is_file():
        die(f"{WEBUI_REQUIREMENTS.name} is missing — is this the full repository?")


def requirement_files(with_desktop):
    files = [WEBUI_REQUIREMENTS]
    if with_desktop and DESKTOP_REQUIREMENTS.is_file():
        files.append(DESKTOP_REQUIREMENTS)
    return files


def fingerprint(with_desktop):
    """Hash of the requirement files, so an unchanged venv is left alone."""
    digest = hashlib.sha256()
    digest.update(f"py{sys.version_info.major}.{sys.version_info.minor}|".encode())
    for path in requirement_files(with_desktop):
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def read_state():
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def write_state(with_desktop):
    STATE_FILE.write_text(
        json.dumps(
            {
                "fingerprint": fingerprint(with_desktop),
                "desktop": bool(with_desktop),
                "python": platform.python_version(),
                "platform": platform.platform(),
            },
            indent=2,
        ),
        encoding="utf-8",
    )


# --- installing ---------------------------------------------------------------
def create_venv():
    banner(f"Creating the venv in {VENV_DIR}")
    try:
        # with_pip mirrors `python -m venv`; some distributions ship venv
        # without ensurepip, which is caught below.
        venv.EnvBuilder(with_pip=True, clear=True, symlinks=os.name != "nt").create(VENV_DIR)
    except Exception as exc:
        die(
            f"Could not create the venv: {exc}\n\n"
            "On Debian or Ubuntu this usually means the venv module is missing:\n"
            f"    sudo apt install python{sys.version_info.major}."
            f"{sys.version_info.minor}-venv"
        )
    if not venv_python().is_file():
        die(f"The venv was created but {venv_python()} is not there.")


def pip_install(args, verbose):
    python = venv_python()
    command = [str(python), "-m", "pip", "install"] + list(args)
    if not verbose:
        command.append("--quiet")
    say(f"  pip install {' '.join(str(a) for a in args)}")
    result = subprocess.run(command, cwd=str(ROOT))
    if result.returncode != 0:
        die(
            "pip failed (see the output above).\n\n"
            "Common causes: no internet connection, a proxy that needs configuring, "
            "or a partially built venv. Try again with --recreate to start clean."
        )


def install_requirements(with_desktop, verbose):
    banner("Installing dependencies into the venv")
    pip_install(["--upgrade", "pip", "setuptools", "wheel"], verbose)
    for path in requirement_files(with_desktop):
        pip_install(["-r", str(path)], verbose)
    write_state(with_desktop)
    say()
    say("Dependencies are installed.")


def ensure_venv(update, recreate, with_desktop, verbose):
    """Build, refresh or reuse the venv. Returns nothing; exits on failure."""
    if recreate and VENV_DIR.exists():
        banner(f"Removing {VENV_DIR}")
        shutil.rmtree(VENV_DIR, ignore_errors=True)

    fresh = not venv_is_usable()
    if fresh:
        if VENV_DIR.exists():
            say("The existing venv looks broken; rebuilding it.")
            shutil.rmtree(VENV_DIR, ignore_errors=True)
        create_venv()

    state = read_state()
    # Asking for --desktop after a web-only install has to trigger a top-up.
    wanted = with_desktop or state.get("desktop", False)
    stale = state.get("fingerprint") != fingerprint(wanted)

    if fresh or update or stale:
        if stale and not fresh and not update:
            banner("The requirements changed since the last install")
        install_requirements(wanted, verbose)
    else:
        say(f"Using the existing venv in {VENV_DIR}")


# --- credentials --------------------------------------------------------------
def run_credential_step(mode, username="", password=""):
    """Create or rotate the login, inside the venv where the hasher lives.

    Runs before the WebUI is ever started. There is no path through this file
    that launches a network listener without a working password verifier and a
    stored credential.
    """
    python = venv_python()
    command = [str(python), "-c", _CREDENTIAL_SCRIPT, mode, username, password]
    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    env.pop("PYTHONPATH", None)
    result = subprocess.call(command, cwd=str(ROOT), env=env)
    if result != 0:
        die(
            "Video Trim will not start without credentials.\n\n"
            "Run the launcher again from a console window and set a username and "
            "password when prompted."
        )


def resolve_cli_password(args):
    """Prefer an environment variable; warn loudly about the literal form."""
    if args.auth_password_env:
        value = os.environ.get(args.auth_password_env, "")
        if not value:
            die(f"{args.auth_password_env} is not set in the environment.")
        return value
    if args.auth_password:
        say(
            "Note: --auth-password can appear in shell history and in process "
            "listings. --auth-password-env is safer."
        )
        return args.auth_password
    return ""


# --- launching ----------------------------------------------------------------
def launch(passthrough):
    python = venv_python()
    command = [str(python), str(ROOT / "webui.py")] + list(passthrough)
    say()
    say("Starting the Video Trim WebUI…")

    env = dict(os.environ)
    # Unbuffered so the URL banner appears immediately in the console window.
    env["PYTHONUNBUFFERED"] = "1"
    env.pop("PYTHONPATH", None)
    try:
        return subprocess.call(command, cwd=str(ROOT), env=env)
    except KeyboardInterrupt:
        return 0


def main():
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--update", action="store_true")
    parser.add_argument("--recreate", action="store_true")
    parser.add_argument("--desktop", action="store_true")
    parser.add_argument("--install", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--change-auth", action="store_true")
    parser.add_argument("--auth-user", default="")
    parser.add_argument("--auth-password", default="")
    parser.add_argument("--auth-password-env", default="")
    args, passthrough = parser.parse_known_args()

    # Keep our own flags — and any values they consumed — out of what webui.py
    # sees. Credentials are used during setup and are never handed to the WebUI
    # as a standing secret.
    passthrough = _strip_our_flags(passthrough)

    say("Video Trim — one-click setup")
    say(f"  folder   {ROOT}")
    say(f"  python   {platform.python_version()} ({sys.executable})")
    say(f"  venv     {VENV_DIR}")

    check_environment()
    ensure_venv(args.update, args.recreate, args.desktop, args.verbose)

    password = resolve_cli_password(args)

    if args.change_auth:
        banner("Changing the Video Trim login")
        run_credential_step("rotate", args.auth_user, password)
        return 0

    # Mandatory: an unsecured WebUI must be impossible in normal setup.
    run_credential_step("ensure", args.auth_user, password)

    if args.install:
        say()
        say("Setup finished. Run the launcher again to start the WebUI.")
        return 0
    return launch(passthrough)


def _strip_our_flags(passthrough):
    kept = []
    skip_next = False
    for token in passthrough:
        if skip_next:
            skip_next = False
            continue
        name = token.split("=", 1)[0]
        if name in OURS:
            continue
        if name in OURS_WITH_VALUE:
            skip_next = "=" not in token
            continue
        kept.append(token)
    return kept


if __name__ == "__main__":
    sys.exit(main())
