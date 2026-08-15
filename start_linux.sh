#!/usr/bin/env bash
# Video Trim WebUI — one-click launcher for Linux.
#
# The first run builds a "venv" folder next to this script and installs
# everything into it; later runs reuse it and start straight away. The WebUI
# opens at http://127.0.0.1:7862
#
#   ./start_linux.sh                 install if needed, then launch
#   ./start_linux.sh --local-only    keep it to this machine only
#   ./start_linux.sh --update        reinstall the dependencies
#
# Flags can also go in CMD_FLAGS.txt so they apply every time.

set -u

cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" || exit 1

echo
echo " Video Trim — one-click launcher"
echo

if [[ "$(pwd)" == *$'\n'* ]]; then
  echo " ERROR: this folder's path contains a newline, which cannot be handled."
  exit 1
fi

# Find a usable Python 3.9+.
PYCMD=""
for candidate in python3 python python3.13 python3.12 python3.11 python3.10 python3.9; do
  if command -v "$candidate" >/dev/null 2>&1 &&
     "$candidate" -c 'import sys;sys.exit(0 if sys.version_info[:2]>=(3,9) else 1)' >/dev/null 2>&1
  then
    PYCMD="$candidate"
    break
  fi
done

if [[ -z "$PYCMD" ]]; then
  cat <<'MSG'
 ERROR: Python 3.9 or newer was not found.

 Install it with your package manager, for example:

     sudo apt install python3 python3-venv     # Debian / Ubuntu
     sudo dnf install python3                  # Fedora
     sudo pacman -S python                     # Arch

 …then run this script again.
MSG
  exit 1
fi

echo " Using $("$PYCMD" -c 'import sys;print(sys.executable)') ($("$PYCMD" -c 'import platform;print(platform.python_version())'))"

exec "$PYCMD" one_click.py "$@"
