"""The Gradio web front end for Video Trim.

Nothing in this package imports Qt: the browser is the window, ffmpeg does the
cutting, and the only shared code with the desktop app is the Qt-free core
(``ffmpeg_tools``, ``naming``, ``paths``, ``timefmt``).
"""
