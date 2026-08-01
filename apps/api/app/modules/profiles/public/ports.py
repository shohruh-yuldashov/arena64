"""`profiles`' published port — one, and read-only.

Satisfied by `BatchProfileRenderer`. See that class and this package's
`__init__` for why the shape is "many players, one asserted relationship"
rather than "one player, one viewer".
"""

from collections.abc import Mapping, Sequence
from typing import Protocol
from uuid import UUID

from app.modules.profiles.domain.profile import PublicProfile
from app.modules.users.public import ViewerRelationship


class ProfileRenderer(Protocol):
    """Renders public profiles with the platform's privacy gate applied.

    **The gate is not optional and not a parameter.** There is no argument
    here that turns it off, no "raw" variant, and no way to reach the
    underlying identity without it — which is what makes it safe to hand to
    a background worker that fans out to other people.

    Batch-only, deliberately. A singular method would be used in a loop by
    the first consumer with a list, and the cost of that is one round trip
    per recipient on a path that runs per event.
    """

    async def render_many(
        self, player_ids: Sequence[UUID], *, relationship: ViewerRelationship
    ) -> Mapping[UUID, PublicProfile]:
        """The public view of each player, as seen by somebody in
        `relationship` to them.

        Keyed by player id, and **players with no public profile are absent
        from the result rather than present and empty**: a deactivated or
        unknown account has no view, and a caller must decide what to do
        about that rather than receive a blank one.

        `relationship` is asserted by the caller and applied to every player
        in the batch, so a caller whose list mixes relationships must make
        more than one call. `ViewerRelationship.BLOCKED` is legal and
        produces the most restrictive view — though a caller that already
        knows a recipient is blocked should not be rendering for them at all.
        """
        ...
