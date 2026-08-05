"""`QueuePool` — which queue a ticket is waiting in.

Framework-free (architecture.md §8): no SQL, no clock, no HTTP.

A64-015.1 spread a pool's identity across two fields on the ticket —
`queue_type` and `region` — and it worked because there was one variant and
nothing scanned. A64-015.2 gives it a name and a third component, for two
reasons that are not stylistic:

1. **A pool is a thing pairing talks about.** A64-015.3's scan reads *one
   pool at a time*; a triple of loose fields threaded through a repository,
   a service, a schema and an index is three chances to pass them in the
   wrong order, and the compiler cannot help with three enums.
2. **The variant has to be on the ticket.** Two players queueing for
   different rule sets are not opponents, and a ticket that does not record
   which game it is waiting for cannot be excluded from the wrong scan. It
   is free to add now, with no ticket in production, and it is a migration
   over live rows later.

## The fourth component, and why it is only an identifier

A64-020.5A-pre closed the gap this file recorded for five tasks: a pool is
`(variant, mode, time control, region)` and now carries all four.

What it carries is `TimeControlId` and **not** the control's durations or
its speed class. That is not an omission — it is what keeps this type pure.
`identifier()` is a wire format a pairing task is dispatched with, so
`from_identifier` has to reconstruct a pool from a string with no database
in reach; and `every_pool()` has to enumerate pools at composition time
without a query. An identifier round-trips through both. A resolved control
would round-trip through neither.

The durations and the speed class live on the **ticket**, as a snapshot —
see `QueueTicket.time_control` and `reference.domain.time_control` on why a
permanent record must not be re-read from a catalogue somebody may edit. A
pool reconstructed from an identifier is used to *select* tickets; the
tickets are what a match is built from.

The prediction this file made held exactly: the definition of "blitz" is
`reference`'s, `matchmaking` never guesses one, and every rating category
inherits the catalogue's answer rather than this module's.

## Deterministic equality, and why it is not incidental

Frozen, slotted and comparing by value, so two pools built from the same
three choices are one key. A pairing scan groups tickets by pool, a metric
is labelled by pool, and a future Redis index would be keyed by
`identifier()` — all three break quietly if equality were by identity.
"""

from dataclasses import dataclass
from enum import StrEnum

from app.modules.game.public import ProductVariant, require_offered
from app.modules.reference.public import TimeControlId


class QueueType(StrEnum):
    """Whether a game in this pool moves a rating.

    The split that changes what a match *means* rather than how it is
    played: a rated game moves a permanent number (A-4), a casual one does
    not.

    A native PostgreSQL enum on the column (DB-15): closed, stable, and on
    a column every pool query filters, so four bytes beats a string and a
    typo cannot become a value no read path knows how to evaluate.
    """

    RANKED = "ranked"
    CASUAL = "casual"


class Region(StrEnum):
    """Where the player is, for the purpose of who they can be paired with.

    AD-25 defers multi-region *infrastructure* and says explicitly what
    replaces it: "pairing players by geography (a matchmaking policy, not
    an infrastructure change) buys more perceived latency improvement than
    any replication topology." This is that policy's input, and it is on
    the ticket from the first release for that reason — a pool that is not
    partitioned by region at entry cannot be partitioned by it later
    without re-queueing everybody.

    **Reference data wearing an enum's clothes, and knowingly so.** DB-08
    puts variants, time controls and locales in a `reference` schema, and a
    region belongs there with them. No `reference` schema exists in code
    yet, and creating one for a single closed list would be the speculative
    generality CLAUDE.md §1.7 rules out. When `reference` arrives this
    becomes `reference.region` and the column a foreign key; the values are
    chosen to survive that move unchanged.

    `GLOBAL` is not a place. It is the answer for a player who has not been
    located and for a pool that does not partition, and it is the default
    precisely so that an unlocated player is pairable with everybody rather
    than with nobody.
    """

    GLOBAL = "global"
    EUROPE = "europe"
    NORTH_AMERICA = "north_america"
    SOUTH_AMERICA = "south_america"
    ASIA = "asia"
    AFRICA = "africa"
    OCEANIA = "oceania"


@dataclass(frozen=True, slots=True)
class QueuePool:
    """One queue: a rule set, a mode, and a place to look for an opponent.

    Two players in the same pool are candidates for each other; two players
    in different pools never are, whatever their ratings. That sentence is
    the whole reason this type exists as a value rather than as three
    arguments.
    """

    variant: ProductVariant
    """Which rules a game from this pool would be played under.

    A `ProductVariant` from `game.public`, never the engine's
    `BoardVariant`: `matchmaking` reaches `game` through its published
    surface (R-1) and reaches the engine not at all (R-2), and the
    distinction between the two enums is what keeps the English 8x8 test
    fixture off the menu — `specs/game-engine/audit.md` §9.
    """

    queue_type: QueueType

    time_control_id: TimeControlId
    """Which clock a game from this pool would be played under.

    An identifier, never the resolved control — see this module's docstring.
    Two players who chose different controls are not opponents even at the
    same speed class, which is why the pool key is this and not
    `SpeedClass`: 3+2 and 5+0 are both blitz and are not the same game.

    Not defaulted, unlike `region`. A64-020.5A-pre §16 forbids a
    production-facing ambiguous default, and the reason is that the honest
    default does not exist: every value here is a real, different game, so a
    caller who omitted it would silently enter a pool they did not pick.
    """

    region: Region = Region.GLOBAL

    def __post_init__(self) -> None:
        # Re-checked here rather than only at the API boundary, because a
        # repository rehydrating a row constructs this directly. If a
        # variant is ever withdrawn, the tickets already written for it
        # fail here loudly instead of being scanned into a pairing for a
        # game the platform no longer runs (BE-06's argument, applied to
        # reference data rather than to a constraint).
        #
        # Normalising as well as checking, and via `str()` rather than
        # `.value`, so the guard is total: a driver or a test that supplies
        # the primitive gets the enum back rather than a record that is a
        # `ProductVariant` by annotation and a `str` in fact. Frozen
        # dataclass, so assignment goes through `object.__setattr__` — the
        # same shape `users.domain.value_objects` uses for the same reason.
        object.__setattr__(self, "variant", require_offered(str(self.variant)))

    def identifier(self) -> str:
        """A stable, primitive name for this pool.

        `"russian_8x8:ranked:blitz_3_2:global"` — ordered widest to
        narrowest, so a sorted list of pools groups by variant, then by
        mode, then by clock, which is the order a scan and a dashboard both
        want.

        The time control sits **before** the region deliberately: it is the
        coarser split. Two players in different regions may still be paired
        once regional pools are merged; two players on different clocks
        never are.

        Used for log context and metric labels today. It is also the shape
        a Redis pool index would be keyed by, which is why it is defined
        here rather than assembled at each call site — A64-015.3 should not
        have to invent one.
        """
        return ":".join(
            (
                self.variant.value,
                self.queue_type.value,
                self.time_control_id.value,
                self.region.value,
            )
        )

    @classmethod
    def from_identifier(cls, identifier: str) -> "QueuePool":
        """The pool `identifier()` produced — A64-015.3.

        A pairing task carries a pool as one primitive string (§13 forbids
        serialising a repository or a framework object into a payload), and
        this is the other half of that round trip. `identifier()` is
        therefore not merely a label any more; it is a wire format, which
        is why the two are defined next to each other.

        Raises `ValueError` on anything that is not one — a wrong field
        count, an unknown mode, region or time control — and
        `VariantNotOffered` on a variant that is no longer on the menu. A
        malformed payload is a bug in the dispatcher rather than user input,
        so it fails loudly at the boundary instead of scanning some default
        pool.

        A **retired** time control still parses, unlike a withdrawn variant.
        That asymmetry is deliberate: retiring a control stops new tickets
        being written for it (`TimeControlCatalogue.require`) and must not
        stop the ones already waiting from being paired or swept. A variant
        withdrawn mid-flight is a game the platform no longer runs at all,
        which is why `require_offered` refuses it here.
        """
        parts = identifier.split(":")
        if len(parts) != 4:
            raise ValueError(f"{identifier!r} is not a queue pool identifier")

        variant, queue_type, time_control_id, region = parts
        return cls(
            variant=require_offered(variant),
            queue_type=QueueType(queue_type),
            time_control_id=TimeControlId(time_control_id),
            region=Region(region),
        )

    def __str__(self) -> str:
        return self.identifier()


def every_pool() -> tuple[QueuePool, ...]:
    """Every pool the platform currently offers, in a stable order.

    The product of the four axes — one variant, two modes, four time
    controls, seven regions — which is **fifty-six** today. Ordered variant,
    then mode, then control, then region, so the list reads the way
    `identifier()` sorts.

    Enumerated from the enums rather than from the catalogue, which is what
    keeps this function pure and its one caller synchronous. The cost is
    that a *retired* control still gets a scheduler; that scanner finds an
    empty pool once its last ticket drains, which is the same cost every
    other empty pool already pays and is strictly better than making the
    composition root query a database to build a task list.

    Its one caller is the composition root, which needs a scheduler per
    pool (`app_factory.build_task_schedulers`). It lives here rather than
    there because the axes are this module's and a second place that
    enumerated them would drift the day a variant ships.

    **It has stopped scaling, and the replacement is known.** A64-015.2
    wrote "fourteen pools is fourteen cheap indexed reads a second; four
    variants and three time controls would be a few hundred, most of them
    empty", and A64-020.5A-pre is the multiplication it predicted: fifty-six
    partial-index probes a second per process, of which at most a handful
    are ever non-empty.

    That is affordable today and is the last release where it obviously is —
    a second variant makes it a hundred and twelve. The fix is unchanged and
    still deliberately not built here: scan only pools that have waiting
    tickets, with one `DISTINCT` over `ix_queue_ticket__pool` feeding the
    dispatcher, which is a change to the *scheduling* of pairing and belongs
    with the work that measures it rather than with the work that widened
    the key.
    """
    return tuple(
        QueuePool(
            variant=variant,
            queue_type=queue_type,
            time_control_id=time_control_id,
            region=region,
        )
        for variant in ProductVariant
        for queue_type in QueueType
        for time_control_id in TimeControlId
        for region in Region
    )


__all__ = ["QueuePool", "QueueType", "Region", "every_pool"]
