import sys
from pathlib import Path

from scripts import sync_version


def test_read_root_version_ignores_release_please_marker(
    tmp_path: Path, monkeypatch
) -> None:
    version_file = tmp_path / "VERSION"
    version_file.write_text("0.1.5 # x-release-please-version\n")
    monkeypatch.setattr(sync_version, "VERSION_FILE", version_file)

    assert sync_version.read_root_version() == "0.1.5"


def test_print_mode_outputs_the_parsed_root_version(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    version_file = tmp_path / "VERSION"
    version_file.write_text("0.1.5 # x-release-please-version\n")
    monkeypatch.setattr(sync_version, "VERSION_FILE", version_file)
    monkeypatch.setattr(sys, "argv", ["sync_version.py", "--print"])

    assert sync_version.main() == 0
    assert capsys.readouterr().out == "0.1.5\n"
