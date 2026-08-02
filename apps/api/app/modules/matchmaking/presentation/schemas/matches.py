"""Wire schemas for the acceptance endpoints — A64-015.4 §7.

Pydantic models at the boundary, mapping from `game.public.PendingMatchView`
and `users.public.PublicUserProfile`. Nothing here is a `game` domain type:
the published view carries an enum and two instants, and what a client
receives is the same information in the shapes JSON has.

## What is deliberately absent, and why each

§7 lists what must not be exposed, and every item on that list is absent by
*construction* rather than by a field somebody remembered to drop —
`PendingMatchView` never carries it in the first place:

    pairing_id              a pairing internal. A client that held it could
                            correlate two players' matches into a picture
                            of who the scan is considering
    queue ticket ids        the same, one level down
    lock and claim state    `reserved_until`, `SKIP LOCKED`, batch sizes:
                            operational detail with no client meaning
    constraint names        never on a wire response anywhere on this
                            platform
    private player data     the opponent preview is `UserSummary`'s three
                            public fields and nothing else

`time_control` is absent too, and that one is a **gap rather than a
policy**. `reference.time_control` (database.md §6.2) does not exist in
code, so a pool is `(variant, mode, region)` and a match created from one
has no time control to report — see `QueuePool` on why inventing a speed
class in `matchmaking` would hand the module least entitled to own it a
grouping key every rating category would inherit. A nullable field that is
always `null` would be a contract claiming time control is optional when in
fact every real match will have one, so there is no field: when
`reference.time_control` ships, `QueuePool`, `CreateMatchRequest`, the match
row and this schema all gain one in a single change.

## The opponent is one lookup, not an N+1

A player has at most one pending match and a match has exactly one
opponent, so the preview is a single batched read of a single id through
`users.public.PublicProfileReader.find_public_profiles`. There is no list
endpoint here and no per-item lookup anywhere in this module — the
composition that would grow into an N+1 is the one this file deliberately
does not do.

Avatars are **not** rendered. That needs the storage provider and the
privacy-gated composition `profiles` owns, and duplicating it here would be
a second, ungated renderer of a player's identity. A client that wants the
full picture reads `GET /profiles/{username}` with the handle below.
"""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.modules.game.public import MatchRecordStatus, PendingMatchView, PlayerSide, ProductVariant
from app.modules.users.public import PublicUserProfile


class OpponentPreview(BaseModel):
    """Just enough of the other player to render a match card.

    Three fields, and the same three `users.public.UserSummary` calls "what
    a list, a search result, or a future match card needs". This is that
    match card, and the fields it omits — email, verification state, join
    date, country, bio — are the ones that would make it a privacy question
    rather than a rendering one.
    """

    model_config = ConfigDict(extra="forbid")

    player_id: UUID
    username: str
    display_name: str | None


class PendingMatchResponse(BaseModel):
    """One match, as one of its two participants sees it.

    Every field is named from the **reader's** seat rather than by side —
    `your_side`, `you_accepted`, `opponent_accepted` — so a client cannot
    render the wrong half by picking the wrong field, and so nothing here
    reveals which physical row a value came from.
    """

    model_config = ConfigDict(extra="forbid")

    match_id: UUID
    status: MatchRecordStatus = Field(
        description=(
            "`pending_acceptance` while the offer is open, and the state it "
            "settled into once it is not — `active` when both of you accepted, "
            "`cancelled` when somebody declined, `expired` when the window "
            "closed. A client that polls after answering sees the outcome "
            "rather than a `404`."
        )
    )

    your_side: PlayerSide = Field(description="Which side you were assigned. `light` moves first.")

    opponent: OpponentPreview | None = Field(
        description=(
            "Who you were paired with. `null` when that account can no longer "
            "be rendered — deactivated between the pairing and this read — "
            "which is the same answer every other endpoint on this platform "
            "gives for a withdrawn account."
        )
    )

    variant: ProductVariant
    rated: bool = Field(description="Whether finishing this match will move your rating.")

    acceptance_deadline: datetime = Field(
        description=(
            "When this offer stops being honoured. An instant rather than a "
            "countdown, so a slow response cannot make a client's timer wrong. "
            "An answer that arrives after it is refused."
        )
    )

    you_accepted: bool
    opponent_accepted: bool = Field(
        description=(
            "Whether the other player has answered yes. Never says whether "
            "they have *declined* — a decline settles the match, and `status` "
            "is where that is reported."
        )
    )

    created_at: datetime = Field(description="When the pairing produced this match.")

    @classmethod
    def of(
        cls, view: PendingMatchView, opponent: PublicUserProfile | None
    ) -> "PendingMatchResponse":
        """The wire view of a pending match, with its opponent beside it.

        The profile is passed in rather than read here: a schema that
        fetched would be a query in the serialisation layer, and the route
        is where the two reads belong so their cost is visible at the call
        site — the same rule `QueueTicketResponse.of` follows.
        """
        return cls(
            match_id=view.match_id,
            status=view.status,
            your_side=view.your_side,
            opponent=(
                None
                if opponent is None
                else OpponentPreview(
                    player_id=opponent.id,
                    username=opponent.username,
                    display_name=opponent.display_name,
                )
            ),
            variant=view.variant,
            rated=view.rated,
            acceptance_deadline=view.acceptance_deadline,
            you_accepted=view.you_accepted,
            opponent_accepted=view.opponent_accepted,
            created_at=view.created_at,
        )
