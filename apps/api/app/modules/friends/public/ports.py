"""The ports other modules may depend on — BE-03's published surface.

`SocialGraphReader` is two methods over one graph. `profiles` needs exactly
these: which of a page of players the viewer is friends with (A64-013.3),
and which players they cannot interact with at all (A64-013.5).

## Why one port rather than two

They are two reads of one graph, consumed together by one module: relationship
resolution asks both on every composition, because a block outranks a
friendship and the answer is wrong without either. Two published ports would
be two things to wire, two adapters to keep in step, and — the part that
matters — two chances for a consumer to hold the friendship half and not the
block half, which is precisely the leak BL-1 forbids.

The *consumer-side* narrowing still happens, in `profiles.application.ports`:
`ViewerRelationshipProvider` and `BlockedPlayersProvider` are separate there,
because the search path needs the exclusion set and not the relationships.

## `PresenceAudience` is a second port, and the split is by *question*

A64-013.7 gave `friends` a consumer that asks something the reader above
cannot express: not "what is this pair to each other" but "who may be told
about this player". That is one composed answer — friends minus blocked —
and publishing it as a port rather than as its two ingredients is what keeps
the subtraction in `friends`, where BL-1 is enforced, instead of in every
consumer that fans anything out.

Handing `notifications` the reader instead would mean handing it the job of
combining the two sets correctly, forever, in every future fan-out. The
first one to forget the subtraction would deliver a presence frame to
somebody who had been blocked, and it would look exactly like working code.

## `PairingExclusions` is a third port, split by *question* again

A64-015.3 gave `friends` a consumer that asks a third thing: not "what is
this pair to each other" and not "who may be told about this player", but
"among these fifty candidates, which pairs must never be formed". BL-2
makes a blocked pair unpairable, and matchmaking is where that rule is
finally enforced.

It could have been a third method on `SocialGraphReader`, and it is not,
for the reason the split above gives: `profiles` needs neither of the other
two questions answered and should not see a method whose cost is quadratic
in a candidate batch. A port is a contract with a consumer, and these are
three consumers asking three things.

`blocked_ids_for` in a loop would have answered it too, at one query per
candidate — the N+1 CLAUDE.md §10.4 names, on the platform's most
latency-sensitive background scan. One query is the whole point of the
port.
"""

from collections.abc import Mapping, Sequence
from typing import Protocol
from uuid import UUID


class SocialGraphReader(Protocol):
    """Answers what the social graph says about one player's relationships.

    **Read-only by construction.** There is no way here to create or end a
    friendship, or to place or lift a block, which is what makes it safe to
    hand to the module that composes every public profile on the platform.

    `friend_ids_among` is **batch-only**, which stops that module calling it
    per row. `blocked_ids_for` is not, because a block set is per *viewer*
    rather than per rendered player — one call answers a whole page however
    long it is.

    Takes `UUID`s — DM-06's `player_id`, the only reference that crosses a
    context boundary. Deliberately not profiles or usernames: a social graph
    has no business receiving a display name.

    **Applies no privacy.** Whether two players are friends is a fact about
    the graph; whether a *field* may be shown to a friend is
    `users.domain.privacy`'s, applied by `PublicProfileComposer`. A port
    that answered both would be a second place a visibility rule lives.
    """

    async def friend_ids_among(self, player_id: UUID, others: Sequence[UUID]) -> set[UUID]:
        """The subset of `others` currently friends with `player_id`.

        **One query for the whole page.** This runs on the composition path,
        so a per-player form would multiply every profile render on the
        platform — the N+1 pattern CLAUDE.md §10.4 names as the single most
        common cause of slow endpoints.

        Returns the *other* players' ids rather than friendship rows,
        because that is what a caller does with the answer: index it.

        Never raises, and an empty `others` returns an empty set without
        touching the database. A friendship that ended is not a friendship:
        only live rows count.
        """
        ...

    async def blocked_ids_for(self, player_id: UUID) -> frozenset[UUID]:
        """Every player this one cannot interact with, in **either**
        direction — A64-013.5.

        Symmetric even though a block is not. BL-1 makes a block
        one-directional and invisible to its subject, but the *visibility*
        consequence runs both ways: a blocker who kept seeing the person
        they blocked would have gained nothing, and a blocked player who
        could still see the blocker would notice the asymmetry. One set,
        both directions, and neither party can tell which applies.

        Per **viewer**, not per rendered player, so one call answers a page
        of any length — which is why this has no batch form and needs none.

        Never raises. An empty set means no blocks, which is the common
        case.
        """
        ...


class PresenceAudience(Protocol):
    """Who may be told that a player's presence changed — A64-013.7.

    Satisfied by `PresenceAudienceService`. Published so that a fan-out
    lives outside `friends` while the *rule* stays inside it.

    **Guarantees exactly one thing: nobody in the returned set is blocked**,
    in either direction. It guarantees nothing about privacy — a member of
    this set may still have no right to see the field being pushed, which is
    `VisibilityLevel`'s question and `PublicProfileComposer`'s to answer.
    A64-013.7 states the same rule as "audience membership does NOT imply
    permission", and the two halves are deliberately in different modules so
    that neither can be mistaken for the other.

    Resolved at **delivery**, never at enqueue. A block placed between the
    two is the case this whole design exists for.
    """

    async def observers_of(self, player_id: UUID) -> frozenset[UUID]:
        """The player's friends, minus everyone either of them has blocked.

        Two set reads whatever the answer's size, and none at all for a
        player with no friends — the common case, and the one where a
        fan-out has nothing to do.

        An empty set means *send nothing*. A caller that treated it as
        "unfiltered" would broadcast a player's comings and goings to the
        platform, which is why this returns the audience rather than a
        predicate to filter with.
        """
        ...


class PairingExclusions(Protocol):
    """Which of these players must never be paired with which — A64-015.3.

    BL-2's half of the block rule, published for the one consumer that
    enforces it. See this module's docstring on why it is a third port.

    **Not an eligibility check.** Whether a player may queue at all is a
    fact about one player, answered by
    `matchmaking.application.eligibility`. This is a fact about a *pair*,
    and it can only be answered where both are in hand — which is the
    pairing scan, and nowhere earlier.
    """

    async def blocked_pairs_among(
        self, player_ids: Sequence[UUID]
    ) -> Mapping[UUID, frozenset[UUID]]:
        """For each of `player_ids`, which **others in the same batch** they
        must not be paired with.

        **Symmetric**, exactly as `blocked_ids_for` is and for the same
        reason: BL-1 makes a block one-directional and invisible, but a
        blocker paired with the person they blocked would have gained
        nothing from blocking them. If either has blocked the other, the
        pair is excluded and both sides of the mapping say so.

        **One query for the whole batch.** A pairing scan reads up to
        `MATCHMAKING_CANDIDATE_BATCH_SIZE` tickets at a time, and a
        per-candidate form would put that many round trips inside a
        background job that runs continuously.

        Confined to the batch: a block against somebody who is not in
        `player_ids` is irrelevant to this scan and is not returned.
        Players with no exclusions are **omitted** rather than mapped to an
        empty set, so the common case — nobody in the pool has blocked
        anybody — is an empty mapping and no allocation per candidate.

        Never raises, and an empty or single-element `player_ids` returns an
        empty mapping without touching the database: one player cannot be a
        pair.

        **Reveals nothing to a player.** The caller uses it to skip a
        candidate, and the skipped pairing is indistinguishable from a pool
        that simply had nobody suitable — which is what BL-1 requires.
        """
        ...
