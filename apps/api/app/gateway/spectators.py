"""Spectating a live match — A64-016.7 §1, §3, §4.

Read-only, over the connection the client already has, through the channel
architecture AD-11 already gives. Nothing here is a second socket, a second
broadcast pipeline or a second snapshot format.

## The policy, and the product decision it stands in for

**Nothing on this platform specifies who may watch a game.** There is no
`show_spectators` privacy flag, no tournament model, no moderation surface
and no configured delay. §1 says that when the policy is undocumented, the
safest minimal default ships and is reported — so this is that default,
stated so the next task changes a predicate rather than rediscovering the
question:

    the match is `active` or finished   a pairing still being accepted is
                                        not a game, so there is nothing to
                                        watch and its existence is not
                                        public
    neither participant blocks them     `friends.public.PairingExclusions`
                                        already answers this for the pairing
                                        scan, and BL-1's reasoning carries
                                        over unchanged: somebody who blocked
                                        a player has gained nothing if that
                                        player can watch them play
    participants may not spectate       they are in the room already, and a
                                        participant on the spectator channel
                                        would receive the delayed, redacted
                                        feed as well as the real one

Everything else is permitted, which is the *permissive* direction — and it is
deliberate rather than accidental: a public game platform's default is that
games are watchable, and the restrictive default (nobody may watch) would
make the feature ship switched off with no way for a product decision to
switch it on without code.

**What is deliberately not invented:** a delay. AD-10 says a spectator feed
*can* be delayed and gives the reason — engine assistance relayed to a player
in real time — but names no interval, and guessing one would be inventing a
competitive-integrity parameter. Events are immediate today, the seam is
capable of delay (see §6 of the spec), and it is recorded as an open product
decision.

## Why spectators are not room members

A spectator subscribes to `gwspec:v1:<match_id>`, not to `gwroom:v1:`. The
room is the *participants'* routing scope: `both_connected` is derived from
it, the move path checks membership in it, and a spectator appearing there
would make a watched game look like one whose players are present.

The two sets are unioned at fan-out time, and only for events that are
spectator-safe.
"""

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, Protocol
from uuid import UUID

from app.gateway.protocol import MessageType
from app.modules.friends.public import PairingExclusions
from app.modules.game.public import MatchRecordStatus, MatchSnapshot

logger = logging.getLogger(__name__)

#: The events an audience may see — §4, §5.
#:
#: An **allowlist**, and the direction matters more than the contents. A
#: denylist would ship every new server-to-client event to spectators by
#: default, so the first participant-only frame anybody adds — a draw offer,
#: a takeback request, an opponent's disconnection notice — would leak until
#: somebody remembered to exclude it. This way the same mistake ships a
#: spectator missing an event, which is visible and harmless.
#:
#: One member today, because `game.move.applied` is the only event that is
#: fanned out at all, and a move is public the moment it is played.
SPECTATOR_SAFE_EVENTS: Final = frozenset({MessageType.MOVE_APPLIED})

#: The match states a spectator may watch — §1.
#:
#: A frozen set rather than a comparison, so a product decision that opened
#: spectating during the acceptance handshake is one line here, at the place
#: that already explains why it is not there today.
SPECTATABLE_STATES: Final = frozenset({MatchRecordStatus.ACTIVE, MatchRecordStatus.COMPLETED})


class SpectatorRefusal(StrEnum):
    """Why a spectator was turned away.

    Three members, and the coarseness is the same disclosure rule the rest
    of this subsystem keeps: `NOT_SPECTATABLE` covers an unknown match and
    one that is not being played, so a client cannot enumerate live match
    identifiers by sending join frames.
    """

    NOT_SPECTATABLE = "not_spectatable"
    """No such match, or it is not in a state that has a game to watch."""

    BLOCKED = "blocked"
    """A participant has blocked this player, or they have blocked a
    participant. BL-1 makes a block invisible, so this is deliberately
    *distinguishable* from `not_spectatable` only to the person refused —
    they already know they blocked somebody."""

    IS_PARTICIPANT = "is_participant"
    """They are playing. Refused rather than silently upgraded, because a
    participant on the spectator channel would receive the same events twice
    and would see the redacted feed beside the real one."""


class SpectatorEligibility(Protocol):
    """Whether one player may watch one match — §1.

    Its own port rather than a method on the snapshot reader, because the
    two answer different questions and are satisfied by different modules:
    `game` knows the match's state and `friends` knows the block graph.
    """

    async def refusal_for(
        self, snapshot: MatchSnapshot, *, player_id: UUID
    ) -> SpectatorRefusal | None:
        """`None` if they may watch, otherwise why not."""
        ...


class BlockAwareSpectatorPolicy:
    """The default policy — see this module's docstring on what it stands
    in for."""

    def __init__(self, exclusions: PairingExclusions) -> None:
        self._exclusions = exclusions

    async def refusal_for(
        self, snapshot: MatchSnapshot, *, player_id: UUID
    ) -> SpectatorRefusal | None:
        """Three checks, cheapest first.

        The block read is a database query and the other two are
        comparisons, so the state and the participation checks go first —
        which also means a request for a match nobody may watch never
        touches `friends` at all.
        """
        if snapshot.status not in SPECTATABLE_STATES:
            return SpectatorRefusal.NOT_SPECTATABLE

        if snapshot.includes(player_id):
            return SpectatorRefusal.IS_PARTICIPANT

        if await self._is_blocked(snapshot, player_id=player_id):
            return SpectatorRefusal.BLOCKED

        return None

    async def _is_blocked(self, snapshot: MatchSnapshot, *, player_id: UUID) -> bool:
        """Whether a block stands between this viewer and either player.

        **Symmetric**, because `blocked_pairs_among` is: BL-1 makes a block
        one-directional and invisible, and a blocker who could still be
        watched by the person they blocked would have gained nothing from
        blocking them. The same reasoning BL-2 already applies to pairing.

        One batched read for all three players rather than two reads, which
        is the shape the port was built for — and a failure is treated as a
        **refusal**, because a block that could not be checked is one that
        might exist, and admitting on a read error would make a Redis blip a
        privacy bypass.
        """
        participants = (snapshot.light_player_id, snapshot.dark_player_id)

        try:
            blocked = await self._exclusions.blocked_pairs_among([player_id, *participants])
        except Exception as exc:  # noqa: BLE001 — refuse rather than admit
            logger.warning("spectator_block_check_failed", extra={"error": type(exc).__name__})
            return True

        against = blocked.get(player_id, frozenset())
        return any(participant in against for participant in participants)


@dataclass(frozen=True, slots=True)
class SpectatorSubscription:
    """One connection watching one match.

    The pair, not just the player, for the reason `RoomMember` is one: a
    viewer with two tabs is two subscriptions, and closing one must not
    remove the other from the fan-out.
    """

    player_id: UUID
    connection_id: UUID


class SpectatorStore(Protocol):
    """Who is watching what — §3.

    **Ephemeral.** §3 forbids persisting spectator identities in PostgreSQL
    and forbids a durable `SpectatorSession` entity, and the reason is the
    same one rooms are ephemeral: a subscription is a claim about a socket
    that exists right now, derived from nothing durable and reconstructible
    by the viewer pressing watch again.
    """

    async def subscribe(
        self, match_id: UUID, subscription: SpectatorSubscription, *, ttl_seconds: int
    ) -> int:
        """Adds a subscription. Returns how many now watch this match.

        The count comes back from the same operation for the reason every
        other store on this tier returns one: "how many are watching" read
        separately has a window that another join lands in.

        Idempotent on `(player, connection)`.
        """
        ...

    async def unsubscribe(self, match_id: UUID, subscription: SpectatorSubscription) -> int:
        """Removes one. Returns how many remain. Idempotent."""
        ...

    async def routes_for(self, match_id: UUID) -> Sequence[SpectatorSubscription]:
        """Everyone watching, for the fan-out's recipient set."""
        ...

    async def unsubscribe_all(self, subscription: SpectatorSubscription) -> Sequence[UUID]:
        """Removes one connection from every match it watches. Returns which.

        The disconnect path, and the reason this store keeps a reverse index
        for the same reason `RoomMemberStore` does: a socket that dropped
        never sends `spectator.leave`.
        """
        ...


__all__ = [
    "SPECTATABLE_STATES",
    "SPECTATOR_SAFE_EVENTS",
    "BlockAwareSpectatorPolicy",
    "SpectatorEligibility",
    "SpectatorRefusal",
    "SpectatorStore",
    "SpectatorSubscription",
]
