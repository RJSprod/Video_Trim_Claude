#!/usr/bin/env bash
# Video Trim WebUI — one-click launcher for macOS.
#
# The first run builds a "venv" folder next to this script and installs
# everything into it; later runs reuse it and start straight away. The WebUI
# opens at http://127.0.0.1:7862
#
#   ./start_macos.sh                 install if needed, then launch
#   ./start_macos.sh --listen        also reachable from the rest of the network
#   ./start_macos.sh --update        reinstall the dependencies
#
# Flags can also go in CMD_FLAGS.txt so they apply every time.

set -u

# macOS ships no readlink -f, so resolve the script directory the portable way.
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)" || exit 1

echo
echo " Video Trim — one-click launcher"
echo

# Find a usable Python 3.9+. The /usr/bin/python3 stub only works once the
# Command Line Tools are installed, which is why the version test matters.
PYCMD=""
for candidate in python3 python3.13 python3.12 python3.11 python3.10 python3.9 \
                 /opt/homebrew/bin/python3 /usr/local/bin/python3; do
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

 Install it with either:

     xcode-select --install          # Apple's Command Line Tools
     brew install python             # Homebrew
     https://www.python.org/downloads/macos/

 …then run this script again.
MSG
  exit 1
fi

echo " Using $("$PYCMD" -c 'import sys;print(sys.executable)') ($("$PYCMD" -c 'import platform;print(platform.python_version())'))"

exec "$PYCMD" one_click.py "$@"
