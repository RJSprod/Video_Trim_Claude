#!/usr/bin/env python3
"""Video Trim — launch with:  python app.py  [optional video file]"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def _missing_pyside():
    print(
        "Video Trim needs PySide6.\n\n"
        "  pip install -r requirements.txt\n\n"
        "…then run 'python app.py' again.",
        file=sys.stderr,
    )
    return 1


def main():
    try:
        import PySide6  # noqa: F401
    except ImportError:
        return _missing_pyside()

    from videotrim.main import main as run

    return run(sys.argv)


if __name__ == "__main__":
    raise SystemExit(main())
