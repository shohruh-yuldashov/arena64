"""A player's aggregate record, and the one thing on a public profile that
is *computed* rather than read.

Framework-free by rule (architecture.md §8). No clock — the counts are
facts about matches already finished, so nothing here needs "now".

## Why `win_rate` is derived here and never stored

It is `wins / games_played`, and the moment it is stored it is a number
that can disagree with the four counts printed beside it. That divergence
is not hypothetical: it happens the first time a match result is corrected,
an account is anonymised, or a backfill recounts a category — any of which
updates the counts through one path and the derived value through another.
domain-model.md makes the same argument for `LeaderboardEntry` ("rank is
not a property of a player"), and it applies to any figure whose inputs
live next to it.

Computing it on read costs one division. The alternative costs a
consistency invariant.

## The definition, which is a product decision rather than arithmetic

`wins / games_played`, with draws in the denominator.

The obvious alternative is chess's *score percentage*,
`(wins + 0.5 × draws) / games_played`, which is what a rating system
actually consumes and what a serious player expects to see. A64-012.1
specifies a field named `win_rate` alongside a separate `draws` count, so
this implements the literal reading: the proportion of games that were
wins.

Recorded rather than assumed, because the two disagree by a lot for a
drawish player — 40 wins, 40 draws and 20 losses is a 40% win rate and a
60% score. Whoever owns the product answer should confirm which the
profile is meant to show before the number is on screens; changing it
afterwards silently rewrites everyone's apparent record.
"""

from dataclasses import dataclass

#: Four decimal places on a ratio in `[0, 1]` — a resolution of one part in
#: ten thousand, which is finer than any plausible number of games and
#: keeps the JSON stable rather than emitting `0.6666666666666666`.
#:
#: Rounded rather than truncated: truncation makes 0.99995 render as
#: 0.9999, which reads as "not quite everything" for a player who has lost
#: nothing.
WIN_RATE_PRECISION = 4


@dataclass(frozen=True, slots=True)
class PlayerStatistics:
    """Aggregate match counts for one player, across every category.

    Frozen: this is a snapshot read for one response, not a mutable
    accumulator. Whatever eventually maintains these counts owns its own
    write model; what crosses into `profiles` is a reading.

    Validated on construction rather than trusted, because the counts will
    eventually arrive from another module across a port, and a `wins`
    greater than `games_played` is the kind of thing a broken backfill
    produces — better a loud failure on one profile than a win rate above
    100% on every screen that renders it.
    """

    games_played: int = 0
    wins: int = 0
    losses: int = 0
    draws: int = 0

    def __post_init__(self) -> None:
        if min(self.games_played, self.wins, self.losses, self.draws) < 0:
            raise ValueError("match counts cannot be negative")

        # Deliberately `!=` rather than `<=`. A total that merely *fits*
        # would let a lost result go unnoticed; the parts of a completed
        # match record are exhaustive, so they must sum exactly.
        if self.wins + self.losses + self.draws != self.games_played:
            raise ValueError(
                f"wins + losses + draws ({self.wins + self.losses + self.draws}) "
                f"must equal games_played ({self.games_played})"
            )

    @property
    def win_rate(self) -> float:
        """The proportion of games won, in `[0, 1]`, to four decimals.

        **Zero for a player who has never played**, not `None` and not an
        error. A new account has won none of the zero games it has played,
        and `0.0` is both true and the value that renders without every
        client writing a null check. The division by zero it would
        otherwise be is the whole reason this is a method rather than a
        stored column.
        """
        if self.games_played == 0:
            return 0.0
        return round(self.wins / self.games_played, WIN_RATE_PRECISION)


#: The record of a player who has finished no matches — every count zero,
#: which `PlayerStatistics`' own default already expresses.
#:
#: Named rather than written as `PlayerStatistics()` at the call site so
#: that "this player has no games" and "this platform has no statistics
#: module yet" are visibly the same value today and can be told apart
#: tomorrow: when `statistics` ships, only the *provider* changes, and a
#: grep for this name finds every place that assumed the empty record.
NO_MATCHES_PLAYED = PlayerStatistics()
