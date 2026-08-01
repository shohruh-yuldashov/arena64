"""Run the repository's import-linter architecture contracts as a test.

`lint-imports` is also a standalone CI gate, but keeping this test means a
contributor who runs `pytest` receives the same architecture feedback before
pushing. The subprocess intentionally exercises the real command, config
lookup, and import graph rather than a separate library code path.
"""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

#: API project root, where ``.importlinter`` lives.
_API_ROOT = Path(__file__).resolve().parents[2]

#: Import graph construction can be slow on a cold CI filesystem, but it must
#: not hang indefinitely.
_TIMEOUT_SECONDS = 300


def _locate_lint_imports() -> str:
    """Return the project's ``lint-imports`` executable when available.

    Prefer the executable beside the running Python interpreter, which keeps
    ``.venv/bin/pytest`` and ``.venv/bin/lint-imports`` on the same environment.
    Fall back to the repository-local virtualenv and finally ``PATH``.
    """
    beside_interpreter = Path(sys.executable).parent / "lint-imports"
    if beside_interpreter.exists():
        return str(beside_interpreter)

    repository_virtualenv = _API_ROOT / ".venv" / "bin" / "lint-imports"
    if repository_virtualenv.exists():
        return str(repository_virtualenv)

    found = shutil.which("lint-imports")
    if found is None:
        pytest.skip(
            "import-linter is not installed (it is a development dependency); "
            "CI runs the standalone architecture gate"
        )
    return found


def test_every_architecture_contract_holds() -> None:
    """Fail with import-linter's complete diagnostic when any contract breaks."""
    result = subprocess.run(
        [_locate_lint_imports()],
        cwd=_API_ROOT,
        capture_output=True,
        text=True,
        timeout=_TIMEOUT_SECONDS,
        check=False,
    )

    assert result.returncode == 0, (
        "architecture contracts are broken; see apps/api/.importlinter\n\n"
        f"{result.stdout}\n{result.stderr}"
    )
