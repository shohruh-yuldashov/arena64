"""The migration history is a line, not a tree — A64-015.4 §quality gates.

`alembic upgrade head` is a deployment step, and a repository with two heads
does not *fail* it — it fails with "Multiple head revisions are present",
which is discovered by whoever is deploying rather than by whoever merged.
Two branches merged in the same week is exactly how that happens, and no
other test on this platform would notice.

Deliberately not a test that *runs* the migrations. That needs a database
and takes seconds, and the contract suite builds its schema from
`Base.metadata` rather than from revisions — so a run here would be
asserting Alembic works rather than that this repository's revisions are
coherent. What is checkable without a database is the shape of the graph,
which is where the branch appears.

The `upgrade -> downgrade -> upgrade` cycle against real PostgreSQL is a
manual quality gate, recorded in the task's report.
"""

import re
from pathlib import Path

_VERSIONS = Path(__file__).resolve().parents[2] / "alembic" / "versions"

_REVISION = re.compile(r'^revision: str = "([^"]+)"', re.MULTILINE)
_DOWN_REVISION = re.compile(r"^down_revision:[^=]*= (.+)$", re.MULTILINE)


def _revisions() -> dict[str, str | None]:
    """Every revision id, mapped to the one it revises.

    Parsed rather than imported: importing a revision module executes it
    against whatever `alembic` context happens to exist, and the question
    here is about the files.
    """
    history: dict[str, str | None] = {}
    for path in _VERSIONS.glob("*.py"):
        source = path.read_text()
        revision = _REVISION.search(source)
        down = _DOWN_REVISION.search(source)
        assert revision is not None, f"{path.name} declares no revision id"
        assert down is not None, f"{path.name} declares no down_revision"
        parent = down.group(1).strip()
        history[revision.group(1)] = None if parent == "None" else parent.strip('"')
    return history


class TestTheMigrationHistory:
    def test_there_is_exactly_one_head(self) -> None:
        """A second head is a merge that produced two tips, and `alembic
        upgrade head` refuses to guess between them."""
        history = _revisions()
        parents = {parent for parent in history.values() if parent is not None}
        heads = sorted(revision for revision in history if revision not in parents)

        assert len(heads) == 1, f"expected one head, found {heads}"

    def test_there_is_exactly_one_base(self) -> None:
        """Two roots would mean two independent histories in one directory,
        which upgrades in an order nobody chose."""
        bases = sorted(revision for revision, parent in _revisions().items() if parent is None)

        assert len(bases) == 1, f"expected one base, found {bases}"

    def test_every_parent_exists(self) -> None:
        """A `down_revision` naming a deleted file makes the whole history
        unwalkable, and the error names the missing id rather than the file
        that referenced it."""
        history = _revisions()
        dangling = sorted(
            f"{revision} -> {parent}"
            for revision, parent in history.items()
            if parent is not None and parent not in history
        )

        assert dangling == []

    def test_no_two_revisions_share_a_parent(self) -> None:
        """The branch, caught one step before it becomes two heads — this is
        the assertion that fails on the *merge* rather than on the deploy.
        """
        seen: dict[str, list[str]] = {}
        for revision, parent in _revisions().items():
            if parent is not None:
                seen.setdefault(parent, []).append(revision)
        branched = {
            parent: sorted(children) for parent, children in seen.items() if len(children) > 1
        }

        assert branched == {}
