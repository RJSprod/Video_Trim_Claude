#!/usr/bin/env bash
# Reinstall the Video Trim WebUI dependencies into the existing venv.
# Use this after pulling new code, or if the venv has gone wrong.
#
# To throw the venv away and rebuild it from scratch instead:
#     ./update_wizard_macos.sh --recreate

cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)" || exit 1
exec ./start_macos.sh --update --install "$@"
