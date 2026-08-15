"""The web front end: an authenticated shell with Video Trim as one tool.

Nothing in this package imports Qt: the browser is the window, ffmpeg does the
cutting, and the only code shared with the desktop app is the Qt-free core
(``ffmpeg_tools``, ``naming``, ``paths``, ``timefmt``) plus the security layer,
which both front ends go through so the host-protection rules cannot differ
between them.
"""
