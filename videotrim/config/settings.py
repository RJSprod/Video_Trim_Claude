"""Typed access to the handful of things the host can configure.

The save location is the only setting with teeth. It is stored as one canonical
path and re-validated by the output gateway on every commit, because the string
being valid when the host chose it says nothing about whether the drive is still
mounted now.
"""

from ..security import fs_boundary
from ..security.fs_boundary import OutputRootError

SAVE_LOCATION = "save_location"
SAVE_LABEL = "save_location_label"

DEFAULT_LABEL = "the host's save folder"


class SettingsService:
    """Reads and writes settings, and answers "where do saves go?" safely."""

    def __init__(self, store):
        self._store = store

    # --- save location -------------------------------------------------------
    @property
    def raw_save_location(self):
        return self._store.get_setting(SAVE_LOCATION, "") or ""

    def save_location(self):
        """The validated output root, or raise OutputRootError.

        Never falls back to the Desktop, the home directory, or anywhere else. A
        silent fallback is how files end up somewhere the host did not choose.
        """
        configured = self.raw_save_location
        if not configured:
            raise OutputRootError(
                "No save location has been chosen yet. The host sets one in Settings."
            )
        return fs_boundary.validate_output_root(configured)

    def save_location_or_none(self):
        try:
            return self.save_location()
        except OutputRootError:
            return None

    def set_save_location(self, path):
        """Vet and store a directory the host picked. Never creates it."""
        validated = fs_boundary.validate_output_root(path)
        self._store.set_setting(SAVE_LOCATION, str(validated))
        return validated

    @property
    def save_label(self):
        return self._store.get_setting(SAVE_LABEL, "") or DEFAULT_LABEL

    def set_save_label(self, label):
        cleaned = str(label or "").strip()
        self._store.set_setting(SAVE_LABEL, cleaned or DEFAULT_LABEL)

    def destination_for(self, host_local):
        """What to show this session about where files land.

        Host-local sessions see the real path. Remote sessions see a label, in
        every surface — body text, toasts, tooltips, job labels, download
        banners, error details — so no host path ever leaves the machine.
        """
        if not host_local:
            return self.save_label
        configured = self.raw_save_location
        return configured or "not chosen yet"

    def is_configured(self):
        return bool(self.raw_save_location)

    # --- generic -------------------------------------------------------------
    def get(self, key, default=None):
        return self._store.get_setting(key, default)

    def set(self, key, value):
        self._store.set_setting(key, value)
