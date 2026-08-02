"""Which rule sets a player may choose — A64-015.2, re-exported.

The types live in `game.domain.variants` and this module publishes them.
That is the shape every other `public/` package on this platform uses —
`friends.public` re-exports `PlayerBlocked`, `users.public` re-exports
`Presence` — and it is what makes a published surface a *surface* rather
than a second copy of the thing behind it.

## Why they moved here in A64-015.5

They were defined in this file until A64-015.5, and the move is a fix
rather than a tidy-up. A64-015.4 put a `ProductVariant` on `MatchRecord` and
on `game`'s five domain events, which made `game.domain -> game.public` a
real edge — and A64-015.5 published the events, which closed the loop:

    game.public.__init__  -> game.public.events
                          -> game.domain.events
                          -> game.public.variants
                          -> game.public.__init__   (partially initialised)

Python raises `ImportError: cannot import name ... from partially
initialized module` for whichever side is entered first, which in practice
was the migration runner. A64-015.4 patched the same class of cycle once, by
having `engine_services` name domain submodules; a second occurrence is the
signal that the direction was wrong rather than the import.

So the dependency now points the way architecture.md §8 requires —
`public` depends on `domain`, never the reverse — and the cycle is not
merely broken but unconstructible.

Nothing about the types changed in the move, and no consumer's import
changed: `from app.modules.game.public import ProductVariant` resolves
exactly as it did.
"""

from app.modules.game.domain.variants import (
    ProductVariant,
    VariantNotOffered,
    board_variant_of,
    is_offered,
    require_offered,
    variant_catalogue,
)

__all__ = [
    "ProductVariant",
    "VariantNotOffered",
    "board_variant_of",
    "is_offered",
    "require_offered",
    "variant_catalogue",
]
