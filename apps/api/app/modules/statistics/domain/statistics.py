"""`PlayerStatistics` — one player's aggregate competitive record.

Framework-free by rule (architecture.md §8). No clock: every number here is
a fact about matches that already finished, so nothing needs "now".

## Why this moved out of `profiles`

A64-012.1 defined a four-count `PlayerStatistics` inside `profiles`,
because `profiles` was the only consumer and there was no statistics
module to put it in. A64-012.6 creates that module, and the type moves with
it — domain-model.md §11.5 is unambiguous that `PlayerStatistics` belongs
to the `statistics` context, and DM-06 makes `player_id` the only reference
that crosses a context boundary.

`profiles` is now purely a consumer: it imports this from
`statistics.public` and composes it, exactly as it already imports
`PublicUserProfile` from `users.public`. Nothing in `profiles` counts a
game.

## A projection, not an entity

domain-model.md DM-03 and §11.5 classify this as a **projection**: "it has
no invariant of its own; every number is a count of something durable".
Every field is rebuildable from match history, and database.md C5 lists
`player_statistics` among the relations that may be truncated and rebuilt
outright.

That classification is what licenses two decisions below that would be
wrong for an entity — the absence of a row being a legitimate state, and
the validation being a *consistency* check on a computed result rather than
a business rule.

## Why this is a frozen dataclass and not a Pydantic DTO

`users.public` publishes Pydantic `BaseResponseDTO` shapes, because those
are wire-shaped views rendered directly into responses. This one is
consumed by another module's **domain** layer — `profiles.domain.
PublicProfile` holds it — and a domain layer that imported Pydantic to hold
a value object would be importing a framework into the one layer
architecture.md §8 keeps framework-free. A frozen dataclass costs nothing
and crosses that boundary cleanly.

## What is deliberately absent

No per-category breakdown, no termination-reason distribution, no
head-to-head, no colour split, no think-time aggregates. database.md §9.5
specifies all of those, keyed `(player_id, rating_category_id)`, and
domain-model.md §11.5 argues for the termination breakdown at length.

They are absent because A64-012.6 specifies a flat nine-field record and
because nothing produces the data — there is no `match.completed` to fold
in. This is the shape a *profile* renders; the shape a statistics module
maintains will be wider, and §9.5's composite key is where it goes. That
widening is additive: this type gains fields, or the module gains a second
type beside it, and `profiles` keeps consuming what it consumes.
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

#: What an unrated player's rating reads as, and therefore the default for
#: both rating fields below.
#:
#: **This must equal `profiles.domain.ratings.STARTING_RATING`**, and today
#: it is a second constant rather than an import, because `statistics` may
#: not import `profiles` — the dependency runs the other way. A test pins
#: the two together (`tests/unit/test_statistics.py`) rather than trusting
#: this comment.
#:
#: The duplication is a symptom worth naming rather than hiding: the
#: platform currently has two notions of "a player's rating" — the
#: per-category `PlayerRatings` a profile shows, and the single headline
#: number below. Neither is computed by anything yet. When a `rating`
#: module ships it owns both, one of them becomes a projection of the
#: other, and this constant moves there.
DEFAULT_RATING = 1500


@dataclass(frozen=True, slots=True)
class PlayerStatistics:
    """Aggregate competitive record for one player, across every category.

    Frozen: this is a snapshot read for one response, not a mutable
    accumulator. Whatever eventually maintains these counts owns its own
    write model; what crosses a context boundary is a reading.

    Validated on construction rather than trusted. The counts arrive from a
    database row that a future rebuild will have written, and a `wins`
    greater than `games_played` is exactly what a broken backfill produces
    — better a loud failure on one profile than a win rate above 100% on
    every screen that renders it. A projection has no business invariant of
    its own (DM-03), but it does have an *arithmetic* one, and that is what
    this checks.
    """

    games_played: int = 0
    wins: int = 0
    losses: int = 0
    draws: int = 0

    current_rating: int = DEFAULT_RATING
    """The player's headline rating.

    **Distinct from the per-category `ratings` block a profile also
    renders**, which comes from `profiles.application.ports.RatingProvider`
    and reports classic, rapid and blitz separately. Neither is computed by
    anything today, and both start at the same number — see `DEFAULT_RATING`
    on why that duplication is recorded rather than resolved here.
    """

    highest_rating: int = DEFAULT_RATING
    """The peak this player has ever reached. domain-model.md §11.5 lists
    "peak rating" among the statistics a projection carries, which is why
    it lives here rather than on a rating aggregate: a peak is a fact
    derived from history, not the current state of anything."""

    current_streak: int = 0
    """The active run, **signed**: positive counts consecutive wins,
    negative counts consecutive losses, and `0` means the last completed
    match was a draw or there is no history.

    One signed integer rather than a pair of counters plus a "kind", because
    the two cannot both be non-zero and a shape that can represent an
    impossible state is a shape somebody will eventually put one in. A
    client renders the sign; the magnitude is the length.
    """

    best_win_streak: int = 0
    """The longest run of consecutive wins this player has ever had. Never
    negative — the losing equivalent is not a statistic anyone asked for,
    and inventing one now would be shape without a consumer."""

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

        if self.highest_rating < self.current_rating:
            # A peak below the present value is not a rounding difference,
            # it is a projection that missed an update. Loud beats a
            # profile quietly claiming a player has never been as good as
            # they are now.
            raise ValueError(
                f"highest_rating ({self.highest_rating}) cannot be below "
                f"current_rating ({self.current_rating})"
            )

        if self.best_win_streak < 0:
            raise ValueError("best_win_streak cannot be negative")

        if self.best_win_streak < max(self.current_streak, 0):
            raise ValueError(
                f"best_win_streak ({self.best_win_streak}) cannot be below the "
                f"active win streak ({self.current_streak})"
            )

    @property
    def win_rate(self) -> float:
        """The proportion of games won, in `[0, 1]`, to four decimals.

        **Derived here and never stored.** The moment it is a column it is
        a number that can disagree with the four counts printed beside it,
        and that divergence is not hypothetical: it happens the first time
        a result is corrected, an account is anonymised, or a backfill
        recounts. Computing it costs one division; storing it costs a
        consistency invariant.

        **Zero for a player who has never played**, not `None` and not an
        error. A new account has won none of the zero games it has played,
        and `0.0` is both true and the value that renders without every
        client writing a null check.

        The definition is `wins / games_played`, with draws in the
        denominator — the proportion of games that were *wins*, not chess's
        score percentage `(wins + 0.5 × draws) / games_played`. A64-012.1
        specified a field named `win_rate` beside a separate `draws` count,
        so this is the literal reading; the two disagree by a lot for a
        drawish player (40/40/20 is a 40% win rate and a 60% score), and
        whoever owns the product answer should confirm which the profile
        means before it is on screens.
        """
        if self.games_played == 0:
            return 0.0
        return round(self.wins / self.games_played, WIN_RATE_PRECISION)


#: The record of a player who has finished no matches — every count zero,
#: both ratings at the starting value, no streaks.
#:
#: **The absence of a row is this value, not an error.** A projection is
#: built by folding in match results (domain-model.md §11.5), so a player
#: who has played nothing has nothing to fold and therefore no row. That is
#: the ordinary state of every account on the day it registers, and it is
#: why `StatisticsReader` never raises for an unknown player.
#:
#: Named rather than written as `PlayerStatistics()` at each call site so a
#: grep finds every place that assumed the empty record — which is what
#: made moving this type out of `profiles` a mechanical change rather than
#: an archaeological one.
NO_MATCHES_PLAYED = PlayerStatistics()
