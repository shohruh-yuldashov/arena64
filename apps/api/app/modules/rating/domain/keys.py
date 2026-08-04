"""What a rating is *of* — `SpeedClass` and `RatingKey`. SPEC-RATING §7.1.

domain-model.md DM-10: *"Ratings are keyed by `(variant, speed class)` from
day one, even if only one variant ships."* Its reasoning is worth repeating
where the key is defined, because the cost is asymmetric and invisible:

> a single-category rating is a single number, and adding a second category
> later means migrating every existing rating, every rating history entry,
> every leaderboard, and every statistic — all of which are permanent
> competitive records that must reconcile exactly (A-4).

## There is no `RatingCategory` entity

SPEC-RATING §7.1 is explicit. A key is a pair of enums the platform already
owns, so a rating row, a leaderboard row and a match all spell it the same
way with nothing to keep in step. A third concept sitting between them would
be a mapping that can disagree with itself.

The name survives in exactly one place — `profiles`' public response, where
it is a shipped API contract — and is a presentation mapping there, not a
domain type. See `rating.public.compatibility`.

## Why all five speed classes exist while one is reachable

SPEC-RATING §8 makes every rated match `CLASSICAL` in v0.5.0. The other four
are storable and unreachable, deliberately: adding an enum member later is a
migration of the one dataset the platform promises never to corrupt, and
carrying five costs a few bytes in a native enum.

**What is deliberately absent is the derivation.** Nothing here turns a
`(base_time, increment)` pair into a class, because the boundaries are an
open product decision (SPEC-RATING OQ-1) and a plausible-looking guess at
them would silently define "blitz" for the whole platform.
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from app.modules.game.public import ProductVariant


class SpeedClass(StrEnum):
    """How fast a game is, as a rating and leaderboard grouping.

    The five names database.md §6.2 already uses for `reference.time_control`
    — adopted rather than reinvented, so the day that catalogue ships there
    is one vocabulary rather than two that have to be mapped.

    A native PostgreSQL enum on the column (DB-15): closed, stable, and on a
    column every rating and leaderboard query filters.
    """

    BULLET = "bullet"
    BLITZ = "blitz"
    RAPID = "rapid"

    CLASSICAL = "classical"
    """The only class reachable in v0.5.0 — SPEC-RATING §8.

    Spelled `classical`, matching database.md §6.2. `profiles` ships
    `classic` on its public response and that spelling is now a
    presentation-layer alias; see `rating.public.compatibility` on why the
    API contract was kept rather than corrected.
    """

    CORRESPONDENCE = "correspondence"


#: The class every rated match belongs to in v0.5.0 — SPEC-RATING §8.
#:
#: A named constant rather than `SpeedClass.CLASSICAL` written at the call
#: sites, so that the day time controls ship there is one place to find, and
#: the thing found says *why* it was a constant rather than looking like a
#: preference somebody had.
#:
#: **Not configuration.** A tier that rated its matches as `BLITZ` would
#: produce ratings incomparable with every other tier's, which is the same
#: argument `STARTING_RATING` makes.
DEFAULT_SPEED_CLASS: Final = SpeedClass.CLASSICAL


@dataclass(frozen=True, slots=True)
class RatingKey:
    """The identity of one rating: a variant and a speed class.

    A value object, not an entity — it has no lifecycle, no identifier of
    its own and nothing to store. Two keys with the same components are the
    same key, which is what makes `frozen=True` here a correctness property
    rather than a style choice: it is used as a dictionary key and as half
    of a database unique constraint.
    """

    variant: ProductVariant
    speed_class: SpeedClass

    @classmethod
    def of(cls, variant: ProductVariant) -> "RatingKey":
        """The key a match of `variant` rates in, today.

        The one place `DEFAULT_SPEED_CLASS` is applied. A caller that
        reached for the constant directly would be a second place that has
        an opinion about what class a match belongs to, and the two would
        drift on the day the derivation becomes real.
        """
        return cls(variant=variant, speed_class=DEFAULT_SPEED_CLASS)

    def __str__(self) -> str:
        """`russian_8x8/classical` — the form a log line and a leaderboard
        cursor use.

        Defined here rather than formatted at call sites so there is one
        spelling of a key on the wire and in an operator's grep.
        """
        return f"{self.variant.value}/{self.speed_class.value}"


__all__ = ["DEFAULT_SPEED_CLASS", "RatingKey", "SpeedClass"]
