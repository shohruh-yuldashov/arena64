"""The `profiles` infrastructure layer — adapters realising the ports in
`application/ports.py`.

Unusual for this platform in owning no storage: `profiles` has no table, no
repository and no migration. Every value it renders belongs to another
context, so what lives here are the two adapters standing in for contexts
that do not exist yet.

Both are named for the *state they represent* rather than for their
temporary status — `UnratedRatingProvider`, not `FakeRatingProvider`. That
is deliberate. A "fake" or "stub" in a production composition root is a
thing somebody eventually ships by accident; these are correct
implementations of "this player has played nothing", which is genuinely
true of every account on the platform today and will remain true of every
new account after `rating` ships.

When those modules arrive, each is replaced by an adapter over that
module's published port and these files are deleted — one line each in
`presentation/dependencies`, and nothing in `application/` or `domain/`
changes.
"""

from app.modules.profiles.infrastructure.unrated_providers import (
    NoMatchesStatisticsProvider,
    UnratedRatingProvider,
)

__all__ = ["NoMatchesStatisticsProvider", "UnratedRatingProvider"]
