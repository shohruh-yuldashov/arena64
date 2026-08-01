"""`PairingExclusionService` — the implementation behind
`friends.public.ports.PairingExclusions`.

One method, one repository, and no ability to change anything. The same
narrowing `SocialGraphReaderService` makes, applied to a consumer that
needs even less: `matchmaking` cannot read a friendship through this,
cannot list who blocked whom, and cannot place or lift a block. It can
learn only that two candidates must not be paired.

## Why a second class rather than a method on the reader beside it

`SocialGraphReaderService` is handed to `profiles`, which composes every
public profile on the platform. A method whose cost is a function of a
*batch* has no consumer there, and adding it would widen the surface the
platform's highest-volume read path can reach for.

The split is by question — see `friends/public/ports.py` — and one class
per published port keeps "which module may do what" answerable by looking
at the composition root.

## No cache, deliberately

`CachedSocialGraphReader` decorates the per-player read because it runs on
every profile composition. This one runs on a background scan whose
frequency an operator sets, and A64-015.3 rules out adding Redis to the
pairing path before anything has been measured. When it is measured, the
seam is here: a decorator over this class, invalidated by the same two
writers (`BlockingService.block` and `.unblock`) that already invalidate
`friends:v1:`.
"""

import logging
from collections.abc import Mapping, Sequence
from uuid import UUID

from app.modules.friends.application.ports import BlockedPlayerRepository

logger = logging.getLogger(__name__)


class PairingExclusionService:
    """Answers which candidates in a batch may not be paired together."""

    def __init__(self, blocks: BlockedPlayerRepository) -> None:
        self._blocks = blocks

    async def blocked_pairs_among(
        self, player_ids: Sequence[UUID]
    ) -> Mapping[UUID, frozenset[UUID]]:
        """Straight delegation — the seam is the point rather than the code.

        `matchmaking` sees a port it can only ask one question through, and
        `friends` keeps the freedom to change how the answer is computed
        without the pairing scan learning that it happened.
        """
        return await self._blocks.blocked_pairs_among(player_ids)
