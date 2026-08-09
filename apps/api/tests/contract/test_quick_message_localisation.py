"""The quick-message localisation contract — A64-023.1 §8.

The one test in this repository that reads both sides of a boundary the
type system cannot cover. The catalogue is a Python `StrEnum`; the display
text is the client's JSON; and the whole design of this feature rests on
the claim that **the server sends an identifier and the receiving client
renders it in its own locale**. Nothing else can check that the two halves
line up.

## Why this is a real failure mode rather than a hypothetical one

`apps/web`'s `TranslationKey` is derived from `uz.json`, so a key present in
one locale file and missing from another is already a compile error there.
What it cannot see is the *catalogue*: adding `sorry` to `QuickMessage` is
one line of Python, it type-checks, its tests pass, and the frame it puts on
the wire renders in every client as the literal string `game.quickMessages.sorry`
— because `lookup` returns the key when it does not resolve, which is the
right behaviour and is exactly what makes the gap silent.

So the assertion runs in the direction the risk runs: every catalogue member
must have a label, in every supported locale. The reverse — a label with no
catalogue member — is deliberately **not** asserted: a removed entry leaves
translations behind, which costs nothing and is the state a locale file is
in for the one commit between a product decision and a translation sweep.

## Why it is skipped rather than failed when the client is absent

`apps/api` is deployable on its own, and a container built from it has no
`apps/web` beside it. A test that failed there would fail for a reason that
is not a defect — the same posture `conftest.py` takes when PostgreSQL is
unreachable.
"""

import json
from pathlib import Path
from typing import Any

import pytest

from app.core.enums import Locale
from app.gateway.quick_messages import QuickMessage

#: Where the client keeps its messages, relative to this file.
#:
#: Resolved from `__file__` rather than from the working directory, so the
#: test passes under `pytest` run from `apps/api`, from the repository root,
#: or from an IDE that picks its own. Four parents up from
#: `apps/api/tests/contract/` is `apps/`.
_APPS = Path(__file__).resolve().parents[3]
_LOCALES = _APPS / "web" / "src" / "shared" / "i18n" / "locales"

#: The subtree the labels live under, as a dotted path.
#:
#: Named once rather than spelled at both call sites, because the failure of
#: getting it wrong in one of them is a test that silently checks nothing.
_NAMESPACE = ("game", "quickMessages")


def _label_key(message: QuickMessage) -> str:
    """One catalogue member as the client's translation key.

    `good_game` becomes `goodGame`, which is not a transformation this
    platform performs anywhere at runtime — the server never sends a key,
    only an identifier. It is spelled out here because the *convention* is
    what has to hold: the client's message tree is camel case throughout
    (`notFound`, `backToLobby`), and a catalogue entry that arrived as
    snake case would either break that convention or need a lookup table.

    A test asserting the convention is what keeps it a convention rather
    than a coincidence.
    """
    head, *rest = message.value.split("_")
    return head + "".join(part.capitalize() for part in rest)


def _messages_for(locale: Locale) -> dict[str, Any]:
    """The `game.quickMessages` subtree of one locale file."""
    path = _LOCALES / f"{locale.value}.json"
    node: Any = json.loads(path.read_text(encoding="utf-8"))
    for part in _NAMESPACE:
        assert isinstance(node, dict), f"{locale.value}.json has no {'.'.join(_NAMESPACE)} object"
        node = node.get(part, {})
    assert isinstance(node, dict)
    return node


@pytest.mark.skipif(not _LOCALES.is_dir(), reason="apps/web is not present beside apps/api")
@pytest.mark.parametrize("locale", list(Locale))
def test_every_catalogue_entry_has_a_label_in_every_supported_locale(locale: Locale) -> None:
    """§8 — the same identifier renders in the receiver's own language.

    Parametrised by locale rather than looping inside one test, so a missing
    Russian label reports as a failing Russian case instead of as one
    failure that has to be read to discover which of three languages it is
    about.

    The labels are asserted to be **non-empty**, which is the one thing
    beyond presence worth checking: an empty string satisfies the client's
    type, resolves through `lookup`, and renders as a blank bubble — a
    missing translation that looks like a delivery bug.
    """
    labels = _messages_for(locale)

    missing = sorted(
        message.value for message in QuickMessage if not labels.get(_label_key(message), "").strip()
    )
    assert not missing, (
        f"{locale.value}.json is missing labels for {missing} — every member of "
        "QuickMessage must render in every supported locale, or the wire "
        "identifier reaches players as its own translation key"
    )
