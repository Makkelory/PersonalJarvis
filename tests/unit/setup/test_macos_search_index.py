"""Installed in LaunchServices is not the same as findable in Spotlight.

The 2026-09-16 report: ``~/Applications/Personal Jarvis.app`` was registered
with LaunchServices (``lsregister -dump`` listed it) yet Spotlight returned
nothing. Spotlight answers from its own per-volume metadata store; these tests
pin that the bundle is imported there and that a volume whose store is broken
is named with the admin repair command instead of passing silently.
"""

from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path

import pytest

from jarvis.setup import macos_app_bundle, macos_search_index
from jarvis.setup.macos_search_index import (
    SpotlightVolumeIssue,
    announce_to_spotlight,
    indexed_bundle_paths,
    parse_df_mount_point,
    parse_mdutil_status,
    request_spotlight_import,
    spotlight_volume_issue,
)

# Verbatim ``mdutil -s /System/Volumes/Data`` output from the affected Mac.
_BROKEN_DATA_VOLUME = "/System/Volumes/Data:\n\tError: unknown indexing state.\n"
_HEALTHY = "/System/Volumes/Data:\n\tIndexing enabled. \n"
_DISABLED = "/Volumes/External:\n\tIndexing disabled.\n"
_DF_DATA = (
    "Filesystem     512-blocks      Used Available Capacity  Mounted on\n"
    "/dev/disk3s5    478724992 373871528  31615632    93%    /System/Volumes/Data\n"
)


class _Result:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


@pytest.fixture
def fake_tools(monkeypatch: pytest.MonkeyPatch):
    """Pretend to be macOS with the metadata tools present; record every argv."""
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(Path, "is_file", lambda self: True)
    calls: list[list[str]] = []
    replies: dict[str, _Result] = {}

    def _run(argv, **_kwargs):
        calls.append(list(argv))
        return replies.get(Path(argv[0]).name, _Result())

    monkeypatch.setattr(subprocess, "run", _run)
    return calls, replies


def test_broken_store_is_recognized_with_its_volume() -> None:
    issue = parse_mdutil_status(_BROKEN_DATA_VOLUME)

    assert issue == SpotlightVolumeIssue(
        "/System/Volumes/Data", "the Spotlight index is not working (unknown indexing state)"
    )
    assert issue.repair_command == (
        "sudo mdutil -i on /System/Volumes/Data && sudo mdutil -E /System/Volumes/Data"
    )


def test_disabled_indexing_is_recognized() -> None:
    issue = parse_mdutil_status(_DISABLED)

    assert issue is not None
    assert issue.volume == "/Volumes/External"
    assert "turned off" in issue.reason


@pytest.mark.parametrize("output", [_HEALTHY, "", "something macOS may print one day\n"])
def test_only_a_reported_failure_counts_as_an_issue(output: str) -> None:
    """A warning must rest on a state macOS reported, never on a guess."""
    assert parse_mdutil_status(output) is None


def test_every_probe_is_inert_off_macos(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(
        subprocess, "run", lambda *_a, **_k: pytest.fail("no metadata tool may run off macOS")
    )
    bundle = Path("/x/Personal Jarvis.app")

    assert request_spotlight_import(bundle) is False
    assert spotlight_volume_issue(bundle) is None
    assert indexed_bundle_paths() is None


def test_import_targets_the_bundle(fake_tools) -> None:
    calls, _replies = fake_tools
    bundle = Path("/Users/u/Applications/Personal Jarvis.app")

    assert request_spotlight_import(bundle) is True
    assert calls == [["/usr/bin/mdimport", str(bundle)]]


def test_a_failed_import_reports_false(fake_tools) -> None:
    _calls, replies = fake_tools
    replies["mdimport"] = _Result(returncode=1, stderr="boom")

    assert request_spotlight_import(Path("/a/Personal Jarvis.app")) is False


def test_a_missing_tool_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(Path, "is_file", lambda self: True)

    def _raise(*_a, **_k):
        raise OSError("no such file")

    monkeypatch.setattr(subprocess, "run", _raise)

    assert request_spotlight_import(Path("/a/Personal Jarvis.app")) is False
    assert spotlight_volume_issue(Path("/a")) is None


def test_announce_warns_with_the_repair_command_on_a_broken_store(
    fake_tools, caplog: pytest.LogCaptureFixture
) -> None:
    calls, replies = fake_tools
    replies["df"] = _Result(stdout=_DF_DATA)
    replies["mdutil"] = _Result(stdout=_BROKEN_DATA_VOLUME)
    bundle = Path("/Users/u/Applications/Personal Jarvis.app")

    with caplog.at_level(logging.WARNING, logger=macos_search_index.__name__):
        assert announce_to_spotlight(bundle) is True

    assert [Path(argv[0]).name for argv in calls] == ["mdimport", "df", "mdutil"]
    assert "sudo mdutil -i on /System/Volumes/Data &&" in caplog.text


def test_mdutil_is_asked_about_the_mount_point_not_the_bundle(fake_tools) -> None:
    """Handed the bundle path, ``mdutil -s`` echoes it back as the "volume"
    (seen live) and the repair command would name the app folder."""
    calls, replies = fake_tools
    replies["df"] = _Result(stdout=_DF_DATA)
    replies["mdutil"] = _Result(stdout=_BROKEN_DATA_VOLUME)

    issue = spotlight_volume_issue(Path("/Users/u/Applications/Personal Jarvis.app"))

    assert calls[-1] == ["/usr/bin/mdutil", "-s", "/System/Volumes/Data"]
    assert issue is not None and issue.volume == "/System/Volumes/Data"


def test_an_unknown_mount_point_reports_no_issue(fake_tools) -> None:
    calls, replies = fake_tools
    replies["df"] = _Result(returncode=1)

    assert spotlight_volume_issue(Path("/nowhere")) is None
    assert [Path(argv[0]).name for argv in calls] == ["df"]


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        (_DF_DATA, "/System/Volumes/Data"),
        (
            "Filesystem 512-blocks Used Available Capacity Mounted on\n"
            "/dev/disk5s1 100 50 50 50%    /Volumes/My Disk\n",
            "/Volumes/My Disk",
        ),
        ("", None),
        ("Filesystem 512-blocks Used Available Capacity Mounted on\n", None),
    ],
)
def test_df_mount_point_parsing(output: str, expected: str | None) -> None:
    assert parse_df_mount_point(output) == expected


def test_announce_is_quiet_on_a_healthy_volume(
    fake_tools, caplog: pytest.LogCaptureFixture
) -> None:
    _calls, replies = fake_tools
    replies["df"] = _Result(stdout=_DF_DATA)
    replies["mdutil"] = _Result(stdout=_HEALTHY)

    with caplog.at_level(logging.WARNING, logger=macos_search_index.__name__):
        announce_to_spotlight(Path("/Users/u/Applications/Personal Jarvis.app"))

    assert caplog.text == ""


def test_indexed_paths_skip_hidden_build_directories(fake_tools) -> None:
    """A stale ``.jarvis-native-*/previous.app`` hit is not what search shows."""
    calls, replies = fake_tools
    replies["mdfind"] = _Result(
        stdout=(
            "/Users/u/Applications/.jarvis-native-abc/previous.app\n"
            "/Users/u/Applications/Personal Jarvis.app\n"
        )
    )

    assert indexed_bundle_paths("com.example.app") == [
        Path("/Users/u/Applications/Personal Jarvis.app")
    ]
    assert calls[0][1] == "kMDItemCFBundleIdentifier == 'com.example.app'"


def test_launch_services_registration_also_imports_into_spotlight(
    tmp_path: Path, fake_tools
) -> None:
    """Every install/repair path goes through this one call; lsregister alone
    left the app registered but unsearchable."""
    calls, replies = fake_tools
    replies["mdutil"] = _Result(stdout=_HEALTHY)
    bundle = tmp_path / "Personal Jarvis.app"

    assert macos_app_bundle.register_with_launch_services(bundle) is True

    tools = [Path(argv[0]).name for argv in calls]
    assert "lsregister" in tools
    assert ["/usr/bin/mdimport", str(bundle)] in calls


def test_a_spotlight_crash_never_blocks_launch_services(
    tmp_path: Path, fake_tools, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls, _replies = fake_tools

    def _boom(_bundle: Path) -> bool:
        raise RuntimeError("mds unavailable")

    monkeypatch.setattr(macos_search_index, "announce_to_spotlight", _boom)

    assert macos_app_bundle.register_with_launch_services(tmp_path / "P.app") is True
    assert [Path(argv[0]).name for argv in calls] == ["lsregister"]
