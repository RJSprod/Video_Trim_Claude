"""The output service, the interrupted-run journal, and the path disclosure rule.

The disclosure tests are deliberately blunt: take every string a remote session
could be shown and assert the host's save path, the host's home directory and
the host's username are not in any of them.
"""

import getpass
import os
from pathlib import Path

import pytest

from videotrim.config.settings import SettingsService
from videotrim.config.store import CommitJournal, Store
from videotrim.security.fs_boundary import (
    CollisionPolicy,
    FilesystemPolicyError,
    OutputRootError,
)
from videotrim.web import shell
from videotrim.web.output import (
    ALREADY_EXISTS,
    NO_SAVE_LOCATION,
    POSSIBLE_PARTIAL,
    OutputService,
    OutputUnavailable,
)


@pytest.fixture
def services(fake_root, output_root):
    store = Store()
    settings = SettingsService(store)
    settings.set_save_location(output_root)
    journal = CommitJournal(store)
    return store, settings, journal, OutputService(settings, journal)


class FakeSession:
    id = "session-1"
    username = "hostuser"
    client_ip = "192.168.1.50"


# --- staging and publication -------------------------------------------------
def test_publish_creates_a_new_file(services, staged, output_root, sentinels):
    _store, _settings, _journal, output = services
    outcome = output.publish(staged, "clip.mp4", CollisionPolicy.UNIQUE_NEW_NAME)
    assert outcome.status == "saved"
    assert (output_root / "clip.mp4").is_file()
    sentinels.assert_unchanged("publishing a new file")


def test_skip_reports_a_skip_not_a_failure(services, staged, output_root, sentinels):
    _store, _settings, _journal, output = services
    outcome = output.publish(staged, "duplicate.jpg", CollisionPolicy.SKIP_BY_NAME)
    assert outcome.status == "already_exists"
    assert outcome.message == ALREADY_EXISTS
    sentinels.assert_unchanged("a skip must not write")


def test_a_journalled_name_gets_the_partial_message_not_the_plain_skip(services, staged,
                                                                      output_root):
    """The distinction that stops somebody trusting a half-written file."""
    _store, _settings, journal, output = services
    journal.record_pending(str(output_root), "duplicate.jpg", "earlier-run")

    outcome = output.publish(staged, "duplicate.jpg", CollisionPolicy.SKIP_BY_NAME)
    assert outcome.status == "possible_partial"
    assert outcome.message == POSSIBLE_PARTIAL
    assert outcome.message != ALREADY_EXISTS


def test_journal_is_cleared_on_a_successful_publication(services, staged, output_root):
    _store, _settings, journal, output = services
    output.publish(staged, "clean.mp4", CollisionPolicy.UNIQUE_NEW_NAME)
    assert not journal.knows_name(str(output_root), "clean.mp4")
    assert journal.survivors() == []


def test_a_surviving_journal_row_is_reported_and_never_acted_on(services, output_root,
                                                                sentinels):
    """A crash leaves an orphan a later run cannot claim. Report only."""
    _store, _settings, journal, _output = services
    journal.record_pending(str(output_root), "duplicate.jpg", "crashed-run")

    survivors = journal.survivors()
    assert len(survivors) == 1
    assert survivors[0]["basename"] == "duplicate.jpg"

    journal.acknowledge(survivors[0]["id"])
    assert journal.survivors() == []
    # Acknowledging removes the note and nothing on disk.
    sentinels.assert_unchanged("acknowledging a notice must not touch any file")
    assert (output_root / "duplicate.jpg").is_file()


def test_no_save_location_means_failure_not_a_fallback(fake_root, staged, sentinels,
                                                       elsewhere):
    store = Store()
    settings = SettingsService(store)
    output = OutputService(settings, CommitJournal(store))

    with pytest.raises(OutputUnavailable):
        output.publish(staged, "clip.mp4", CollisionPolicy.UNIQUE_NEW_NAME)

    home_landings = list(Path.home().glob("clip.mp4"))
    assert not home_landings, "an unconfigured save location fell back to the home dir"
    sentinels.assert_no_new_files(elsewhere)


def test_setting_a_save_location_inside_the_install_is_refused(fake_root):
    store = Store()
    settings = SettingsService(store)
    with pytest.raises(OutputRootError) as excinfo:
        settings.set_save_location(fake_root / "cache")
    assert "Choose a folder outside it" in str(excinfo.value)
    assert not settings.is_configured()


def test_changing_the_save_location_never_migrates_anything(services, staged,
                                                            output_root, tmp_path,
                                                            sentinels):
    _store, settings, _journal, output = services
    output.publish(staged, "first.mp4", CollisionPolicy.UNIQUE_NEW_NAME)

    second = tmp_path / "second_output"
    second.mkdir()
    settings.set_save_location(second)

    assert (output_root / "first.mp4").is_file(), "changing the setting moved a file"
    assert not (second / "first.mp4").exists()
    sentinels.assert_unchanged("changing the save location")


# --- jobs --------------------------------------------------------------------
def test_a_render_job_cannot_target_an_external_path(fake_root, output_root):
    from videotrim.web.jobs import JobRegistry

    registry = JobRegistry()
    with pytest.raises(FilesystemPolicyError):
        registry.start("clip", "label", 1000, ["ffmpeg"], output_root / "direct.mp4")


def test_a_failed_job_only_removes_its_internal_staging(fake_root, output_root,
                                                        sentinels):
    from videotrim.web.jobs import Job

    staging = fake_root / "cache" / "exports" / "failing"
    staging.mkdir(parents=True)
    target = staging / "clip.mp4"
    target.write_bytes(b"partial")

    job = Job("id", "clip", "label", 1000, ["ffmpeg"], target)
    job._fail("boom")

    assert not target.exists(), "the internal partial was not cleaned up"
    sentinels.assert_unchanged("a failed job must not touch anything external")


# --- path disclosure ---------------------------------------------------------
def _remote_strings(settings, output, journal):
    """Everything a remote session can be shown, gathered in one place."""
    class Decision:
        allowed = False
        reason = "File transfer disabled by host"
        can_request = True
        requested = False

    capabilities = shell.capabilities(
        FakeSession(), host_admin=False, can_browse=False,
        can_write=Decision(), settings=settings, journal=journal,
    )

    strings = []

    def walk(value):
        if isinstance(value, str):
            strings.append(value)
        elif isinstance(value, dict):
            for item in value.values():
                walk(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                walk(item)

    walk(capabilities)
    strings.append(output.destination_for(host_local=False))
    strings.append(settings.destination_for(host_local=False))
    strings.append(NO_SAVE_LOCATION)
    strings.append(ALREADY_EXISTS)
    strings.append(POSSIBLE_PARTIAL)
    strings.extend([
        shell.home_page("1"), shell.transfer_page("1"), shell.login_page("1"),
    ])
    return strings, capabilities


def test_no_remote_visible_string_contains_a_host_path(services, output_root):
    _store, settings, journal, output = services
    strings, capabilities = _remote_strings(settings, output, journal)

    forbidden = [str(output_root), str(Path.home())]
    try:
        forbidden.append(getpass.getuser())
    except Exception:  # pragma: no cover - unusual environments
        pass
    if os.name == "nt":  # pragma: no cover - Windows
        forbidden.append(str(output_root)[:2])

    for text in strings:
        for secret in forbidden:
            if not secret or len(secret) < 3:
                continue
            assert secret not in text, (
                f"a remote-visible string leaked {secret!r}:\n{text[:400]}"
            )

    assert "output_path" not in capabilities, (
        "the capabilities response handed a filesystem path to a remote session"
    )
    assert capabilities["output_display_name"] == "the host's save folder"


def test_the_host_does_get_the_real_path(services, output_root):
    _store, settings, journal, output = services

    class Decision:
        allowed = True
        reason = "Host machine."
        can_request = False
        requested = False

    capabilities = shell.capabilities(
        FakeSession(), host_admin=True, can_browse=True,
        can_write=Decision(), settings=settings, journal=journal,
    )
    assert capabilities["output_path"] == str(output_root)
    assert output.destination_for(host_local=True) == str(output_root)


def test_remote_sessions_get_no_settings_card(services):
    _store, settings, journal, _output = services

    class Decision:
        allowed = False
        reason = ""
        can_request = True
        requested = False

    capabilities = shell.capabilities(
        FakeSession(), host_admin=False, can_browse=False,
        can_write=Decision(), settings=settings, journal=journal,
    )
    assert capabilities["settings_count"] == 0
    settings_card = [t for t in capabilities["tools"] if t["id"] == "settings"][0]
    assert not settings_card["enabled"]


def test_the_tbd_card_has_no_route(services):
    _store, settings, journal, _output = services

    class Decision:
        allowed = True
        reason = ""
        can_request = False
        requested = False

    capabilities = shell.capabilities(
        FakeSession(), host_admin=True, can_browse=True,
        can_write=Decision(), settings=settings, journal=journal,
    )
    tbd = [t for t in capabilities["tools"] if t["id"] == "tbd"][0]
    assert not tbd["enabled"]
    assert tbd["route"] == "", "the coming-soon card points somewhere"
