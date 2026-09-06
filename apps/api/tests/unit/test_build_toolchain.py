"""The Node version is stated once, and every build reads that one statement.

`.nvmrc` is the statement. CI reads it through `node-version-file`; the
application images read it by naming the same version in their `FROM`.

This test exists because that agreement broke and cost a release candidate.
`f7df1f1` had already diagnosed the failure — Node 22 and Node 24 ship npm
versions that resolve `@tailwindcss/oxide-wasm32-wasi`'s *bundled*
dependencies at different patches, so a lock file written by one is refused
by the other with `Missing: @emnapi/core@1.11.3 from lock file` — and fixed
it in `.github/workflows/ci.yml`. The Dockerfiles kept their own hardcoded
`node:22`, nothing compared the two numbers, and so CI stayed green while
the web image could not be built at all.

That is the shape worth guarding: not "is the version correct", which no
test can know, but "does every place that names it name the same one".
"""

import re
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[4]
NVMRC = _REPO / ".nvmrc"

#: Every image build that runs npm. Listed rather than globbed: a new client
#: application should have to appear here deliberately, and a glob that finds
#: nothing is a test that passes by accident.
DOCKERFILES = (
    _REPO / "apps" / "web" / "Dockerfile",
    _REPO / "apps" / "admin" / "Dockerfile",
)

_FROM_NODE = re.compile(r"^FROM\s+node:(?P<tag>\S+)", re.MULTILINE)


def _pinned_version() -> str:
    return NVMRC.read_text().strip()


def _base_images(dockerfile: Path) -> list[str]:
    return [m.group("tag") for m in _FROM_NODE.finditer(dockerfile.read_text())]


class TestTheVersionIsStatedOnce:
    def test_nvmrc_pins_an_exact_version(self) -> None:
        assert re.fullmatch(r"\d+\.\d+\.\d+", _pinned_version()), (
            f".nvmrc reads {_pinned_version()!r}. A floating major resolves to whatever "
            "the runner image happens to carry, which is how the lock file came to mean "
            "two different things — see f7df1f1."
        )

    @pytest.mark.parametrize("dockerfile", DOCKERFILES, ids=lambda p: p.parent.name)
    def test_the_image_builds_on_the_pinned_version(self, dockerfile: Path) -> None:
        images = _base_images(dockerfile)
        assert images, f"{dockerfile} builds from no node image; update DOCKERFILES"

        pinned = _pinned_version()
        for tag in images:
            assert tag.startswith(f"{pinned}-"), (
                f"{dockerfile.relative_to(_REPO)} builds on node:{tag}, but .nvmrc pins "
                f"{pinned}. The two ship different npm versions, and `npm ci` refuses a "
                "lock file written by the other one."
            )

    def test_no_other_dockerfile_names_a_node_image(self) -> None:
        """A new client must join `DOCKERFILES`, not quietly pick its own version."""
        listed = set(DOCKERFILES)
        strays = [
            path
            for path in _REPO.glob("apps/*/Dockerfile")
            if path not in listed and _base_images(path)
        ]
        assert not strays, (
            "these build from a node image but are not checked against .nvmrc: "
            + ", ".join(str(p.relative_to(_REPO)) for p in strays)
        )
