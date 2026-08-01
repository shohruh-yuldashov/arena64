"""The architecture gate, run as a test.

`lint-imports` is already a CI gate in its own right
(`docs/02-development/testing.md`). Running it here as well is deliberate:
the engine's whole guarantee is that it imports nothing (AD-13), and a
contributor who runs `pytest` and sees green should not have to know that a
separate command is what would have caught the `import datetime` they just
added to a rules function.

The subprocess costs a couple of seconds and reads the real import graph —
there is no way to assert this property from inside the interpreter that
has already imported everything.
"""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

API_ROOT = Path(__file__).resolve().parents[2]


def _locate_lint_imports() -> str | None:
    """The `lint-imports` executable, whether or not the virtualenv is on
    `PATH`.

    `shutil.which` alone finds it only in an activated shell, so a
    contributor running `.venv/bin/pytest` would get a skip — and a gate
    that silently skips is worse than no gate, because the green is
    indistinguishable from a pass.
    """
    beside_interpreter = Path(sys.executable).parent / "lint-imports"
    if beside_interpreter.exists():
        return str(beside_interpreter)
    return shutil.which("lint-imports")


lint_imports = _locate_lint_imports()


@pytest.mark.skipif(lint_imports is None, reason="import-linter is not installed")
def test_every_architecture_contract_holds() -> None:
    """Including `engine-is-a-dependency-free-kernel`, which is what keeps
    the rules kernel testable, mirrorable in TypeScript (AD-14) and movable
    to a worker (AD-13.3)."""
    assert lint_imports is not None
    result = subprocess.run(
        [lint_imports],
        cwd=API_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
