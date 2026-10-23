"""Exercise the PR-title gate as a command-line consumer."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_pr_title.py"


@pytest.mark.parametrize(
    "title",
    [
        "fix(blob): preserve downloaded bytes",
        "feat!: change the public interface",
        "chore(main): release multicloud-py 0.1.9",
        "docs: clarify onboarding",
        "build(deps): update a dependency",
    ],
)
def test_accepts_release_compatible_titles(title: str) -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), title], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "title",
    [
        "Update implementation",
        "fix:",
        "fix:   ",
        "unknown: missing release type",
        "fix: first line\nfeat: injected second line",
        "fix: first line\rsecond line",
    ],
)
def test_rejects_titles_without_a_single_conventional_subject(title: str) -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), title], capture_output=True, text=True
    )
    assert result.returncode == 1
    assert "PR title" in result.stderr


def test_treats_shell_metacharacters_as_title_text(tmp_path: Path) -> None:
    marker = tmp_path / "must-not-exist"
    title = f"docs: explain $(touch {marker})"
    result = subprocess.run(
        [sys.executable, str(SCRIPT), title], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    assert not marker.exists()
