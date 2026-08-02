"""Which rule sets a player may choose — A64-015.2.

Split out of `public/__init__.py` by A64-015.3, which added a second topic
to this package; `friends/public/` already keeps its types in submodules and
re-exports them, and one file per question is why that reads well. Nothing
here changed in the move.

## Why `ProductVariant` is not `BoardVariant`

`BoardVariant` has three members and one of them, `ENGLISH_8X8`, is a
**testing and configuration fixture** rather than a product (recorded in
`specs/game-engine/audit.md` §9). It exists because it is the only second
value three rule axes have, and because the published English draughts
perft series is the engine's only external oracle — so it cannot be
deleted, and it must not be offered.

A validator on a `BoardVariant` field would keep it out of *responses* and
still publish it in the OpenAPI schema as an accepted value. A separate
enum keeps it out of both.

The two are **not** two identifiers: every `ProductVariant` value is a
`BoardVariant` value, `board_variant_of` is the only conversion, and a test
asserts the mapping is total and that `english_8x8` is absent from it.
"""

from enum import StrEnum

from app.core.exceptions import ValidationError
from app.modules.engine import BoardVariant


class ProductVariant(StrEnum):
    """A rule set a player may choose to play.

    Values are `BoardVariant` values, deliberately — one identifier for one
    rule set, so a stored ticket, a wire payload and an engine call all
    spell it the same way.
    """

    RUSSIAN_8X8 = "russian_8x8"
    """The platform's variant. architecture.md A-1's "checkers/draughts with
    mandatory capture and multi-jump moves"."""


class VariantNotOffered(ValidationError):
    """A variant that is not on the menu — A64-015.2.

    A `422` rather than a `404`: the request named something that is not a
    choice, which is malformed input rather than a missing resource. The
    message names the variants that *are* offered, because a client that
    guessed wrong needs the list and there is nothing sensitive in it.
    """


def variant_catalogue() -> tuple[ProductVariant, ...]:
    """Every variant a player may select, in a stable order.

    A tuple rather than the enum itself, so a caller rendering a menu is
    not handed something it could iterate in a different order on a
    different day — and so this can later become a filtered view (a
    variant disabled for maintenance) without every caller changing.
    """
    return tuple(ProductVariant)


def is_offered(variant: str) -> bool:
    """Whether `variant` names something a player may choose."""
    return variant in {member.value for member in ProductVariant}


def require_offered(variant: str) -> ProductVariant:
    """`variant` as a product choice, or `VariantNotOffered`.

    The single gate. `english_8x8` fails here even though the engine plays
    it perfectly well, which is the whole point of the distinction.
    """
    if not is_offered(variant):
        offered = ", ".join(member.value for member in variant_catalogue())
        raise VariantNotOffered(
            f"{variant!r} is not an available variant. Choose one of: {offered}."
        )
    return ProductVariant(variant)


def board_variant_of(variant: ProductVariant) -> BoardVariant:
    """The engine rule set behind a product choice.

    The **only** conversion between the two, so the day a product variant
    maps to something other than its like-named `BoardVariant` there is one
    place it happens.
    """
    return BoardVariant(variant.value)


__all__ = [
    "ProductVariant",
    "VariantNotOffered",
    "board_variant_of",
    "is_offered",
    "require_offered",
    "variant_catalogue",
]
