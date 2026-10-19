"""Install the built wheel set in isolation and import each distribution's packages."""

from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

from packaging.utils import parse_wheel_filename


def _modules(wheel: Path) -> set[str]:
    modules: set[str] = set()
    with zipfile.ZipFile(wheel) as archive:
        for path in archive.namelist():
            if not path.endswith("/__init__.py") or not path.startswith("multicloud/"):
                continue
            module = path.removesuffix("/__init__.py").replace("/", ".")
            if ".async_blob" not in module:
                modules.add(module)
    return modules


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("artifact_directory", type=Path)
    args = parser.parse_args()
    wheels = sorted(args.artifact_directory.glob("*.whl"))
    if len(wheels) != 21:
        raise SystemExit(f"expected 21 wheels, found {len(wheels)}")
    with tempfile.TemporaryDirectory(prefix="multicloud-py-release-") as directory:
        for wheel in wheels:
            name = str(parse_wheel_filename(wheel.name)[0])
            venv = Path(directory) / name
            subprocess.run([sys.executable, "-m", "venv", venv], check=True)
            python = venv / "bin" / "python"
            subprocess.run(
                [
                    python,
                    "-m",
                    "pip",
                    "install",
                    "--disable-pip-version-check",
                    "--quiet",
                    "--find-links",
                    args.artifact_directory,
                    wheel,
                ],
                check=True,
            )
            script = "import importlib; " + "; ".join(
                f"importlib.import_module({module!r})"
                for module in sorted(_modules(wheel))
            )
            subprocess.run([python, "-I", "-c", script], check=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
