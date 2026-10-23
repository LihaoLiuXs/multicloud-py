"""Validate the single-line subject used by squash merges and Release Please."""

from __future__ import annotations

import argparse
import re
import sys

TITLE = re.compile(
    r"(?:feat|fix|perf|refactor|revert|docs|style|test|build|ci|chore)"
    r"(?:\([a-z0-9._/-]+\))?!?: [^\s\r\n][^\r\n]*"
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("title")
    args = parser.parse_args()
    if TITLE.fullmatch(args.title):
        return 0
    print(
        "PR title must be '<type>[(scope)][!]: <description>' on one line. "
        "Types: feat, fix, perf, refactor, revert, docs, style, test, build, ci, chore.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
