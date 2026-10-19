#!/usr/bin/env python3
"""Propagate the root VERSION file to every distribution's pyproject.toml.

The repo's source of truth for the SDK version is the top-level ``VERSION``
file. Each of the 21 distributions in ``packages/*/*/pyproject.toml`` must
declare that same version twice over:

  1. Its own ``project.version`` line.
  2. Every ``multicloud-py-*>=X.Y.Z`` inter-distribution dependency pin
     in its ``project.dependencies`` list.

Setuptools' ``[tool.setuptools.dynamic]`` cannot read files outside the
package directory (it enforces ``_assert_local``), so a true "read from
root file at build time" mechanism is not available without switching
build backends. Instead, we keep static ``version = "X.Y.Z"`` lines in
every pyproject and use this script to keep them all in lockstep with
the root ``VERSION``.

Modes
-----
``--check``  Exit non-zero if any pyproject diverges from VERSION. Used
             by CI to fail builds where a developer hand-edited a
             version without re-running the sync.

``--write``  Rewrite every pyproject in place so it matches VERSION.
             Run this after bumping VERSION.

``--print``  Print the parsed version value for shell and CI consumers.

Bumping a release
-----------------
::

    printf '%s\n' '0.2.0 # x-release-please-version' > VERSION
    uv run python scripts/sync_version.py --write
    git add VERSION packages/*/*/pyproject.toml
    git commit -m "chore(release): bump to 0.2.0"
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
VERSION_FILE = REPO_ROOT / "VERSION"
PACKAGES_GLOB = "packages/*/*/pyproject.toml"

# Matches ``version = "0.1.0"`` on its own line under ``[project]``.
PROJECT_VERSION_RE = re.compile(r'^version\s*=\s*"([^"]+)"\s*$', re.MULTILINE)

# Matches ``"multicloud-py-<name>>=0.1.0"`` (inter-distribution pin).
# Captures (prefix-up-to-and-including-``>=``, version, trailing-quote-and-comma-or-end).
DEP_PIN_RE = re.compile(r'("multicloud-py-[a-z-]+>=)([0-9][^"]*)(")')


def read_root_version() -> str:
    if not VERSION_FILE.exists():
        sys.exit(f"error: {VERSION_FILE} does not exist")
    version = VERSION_FILE.read_text().strip()
    value, separator, annotation = version.partition("#")
    if separator and annotation.strip() == "x-release-please-version":
        version = value.strip()
    if not version:
        sys.exit(f"error: {VERSION_FILE} is empty")
    return version


def sync_file(path: Path, target_version: str, write: bool) -> list[str]:
    """Return a list of human-readable diffs. Empty list means in sync."""
    original = path.read_text()
    updated = original

    diffs: list[str] = []

    def _replace_project_version(match: re.Match[str]) -> str:
        current = match.group(1)
        if current != target_version:
            diffs.append(f"  project.version: {current} -> {target_version}")
        return f'version = "{target_version}"'

    updated = PROJECT_VERSION_RE.sub(_replace_project_version, updated, count=1)

    def _replace_dep_pin(match: re.Match[str]) -> str:
        prefix, current, suffix = match.group(1), match.group(2), match.group(3)
        if current != target_version:
            diffs.append(f"  {prefix}{current}{suffix} -> {target_version}")
        return f"{prefix}{target_version}{suffix}"

    updated = DEP_PIN_RE.sub(_replace_dep_pin, updated)

    if diffs and write:
        path.write_text(updated)
    return diffs


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--check",
        action="store_true",
        help="Exit non-zero if any pyproject diverges (CI gate).",
    )
    mode.add_argument(
        "--write",
        action="store_true",
        help="Rewrite pyprojects in place so they match VERSION.",
    )
    mode.add_argument(
        "--print",
        dest="print_version",
        action="store_true",
        help="Print the parsed root version.",
    )
    args = parser.parse_args()

    target = read_root_version()
    if args.print_version:
        print(target)
        return 0

    pyprojects = sorted(REPO_ROOT.glob(PACKAGES_GLOB))
    if not pyprojects:
        sys.exit(f"error: no pyproject.toml matched {PACKAGES_GLOB}")

    any_drift = False
    for path in pyprojects:
        diffs = sync_file(path, target, write=args.write)
        if diffs:
            any_drift = True
            rel = path.relative_to(REPO_ROOT)
            verb = "Updated" if args.write else "Drift in"
            print(f"{verb} {rel}")
            for d in diffs:
                print(d)

    if args.check and any_drift:
        print(
            "\nerror: pyproject versions diverge from VERSION; "
            "run `uv run python scripts/sync_version.py --write`",
            file=sys.stderr,
        )
        return 1

    if args.write and not any_drift:
        print(f"All {len(pyprojects)} pyprojects already match VERSION ({target}).")
    elif args.check and not any_drift:
        print(f"OK: all {len(pyprojects)} pyprojects match VERSION ({target}).")

    return 0


if __name__ == "__main__":
    sys.exit(main())
