"""Glicko-2 — the arithmetic, and nothing else. SPEC-RATING §7.2, §7.4.

Mark Glickman, *"Example of the Glicko-2 system"* (2013). Every step below is
numbered against that paper so a reader can check the implementation against
the source rather than against this file's opinion of it.

Pure: no clock, no database, no logging, no configuration. A rating update is
a function of two triples, an outcome and an elapsed duration, and this
module is that function. That is what makes Glickman's published worked
example runnable as a test — see `tests/unit/test_glicko2.py`.

## The scale conversion, and why it is not an implementation detail

Glicko-2 works on an internal scale where a rating of 1500 is 0 and one RD
point is `1/173.7178`. Ratings are *stored* on the familiar scale because
that is what a player reads and what every other system on this platform
compares against. The conversion happens at the boundary of one function, so
nothing outside this module ever holds a μ or a φ.

## Why the volatility step is a solver

Step 5 has no closed form. Glickman specifies the **Illinois algorithm**, a
regula-falsi variant, and gives the convergence tolerance. It is implemented
as written rather than replaced with bisection or Newton: the paper's worked
example pins the output to six decimal places, and a different solver reaches
a different last digit, which would make the one test that proves this is
Glicko-2 unable to prove it.

The iteration cap is a safety bound, not part of the algorithm. Glickman's
procedure converges in a handful of iterations for every input this platform
can produce; the cap exists so that a pathological input cannot hang a worker
holding a database transaction.

## Inactivity — SPEC-RATING §7.4

The paper inflates RD once per elapsed rating period. This platform has no
periods (ADR-001), so inflation is computed from elapsed *time* at the moment
of the next match, using step 6's formula with a fractional period count.

The **ceiling** is this platform's addition and is load-bearing: without it
RD grows without bound over a long absence, and a returning player's first
match moves their rating almost arbitrarily. The ceiling is the initial RD,
which is the honest maximum — "we know nothing about this player" cannot be
truer than for somebody who has never played.
"""

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

#: The rating a player starts with — SPEC-RATING §7.2.
#:
#: 1500 is the conventional origin. A domain constant rather than a setting:
#: two tiers with different origins produce incomparable ratings, and the
#: whole point of a rating is that it compares.
INITIAL_RATING: Final = 1500.0

#: The uncertainty a player starts with, and the ceiling inflation respects.
INITIAL_DEVIATION: Final = 350.0

#: How erratic a player's results are assumed to be before any are seen.
INITIAL_VOLATILITY: Final = 0.06

#: Glickman's τ — how much volatility may move in one update.
#:
#: 0.5 sits at the conservative end of the paper's recommended 0.3–1.2.
#: Deliberately conservative because this platform runs one match per
#: nominal period rather than the 10–15 games Glickman assumes, which makes
#: each volatility estimate noisier — see ADR-001's accepted consequences.
SYSTEM_CONSTANT_TAU: Final = 0.5

#: The scale factor between the published rating and Glicko-2's internal one.
_GLICKO2_SCALE: Final = 173.7178

#: Glickman's convergence tolerance for the volatility solver.
_CONVERGENCE_TOLERANCE: Final = 1e-6

#: A bound on the solver, not part of the algorithm. See this module's
#: docstring: it exists so a pathological input cannot hang a worker that is
#: holding a transaction open.
_MAX_ITERATIONS: Final = 100

#: How long one nominal rating period is, for inactivity only — SPEC-RATING
#: §7.4. There are no rating periods; this is the clock RD inflation uses.
RATING_PERIOD_DAYS: Final = 1.0

_SECONDS_PER_DAY: Final = 86_400.0


@dataclass(frozen=True, slots=True)
class Glicko2Rating:
    """A rating as Glicko-2 models it: a value, an uncertainty, a volatility.

    Frozen, because every operation produces a *new* rating rather than
    mutating one. That is not a style preference: an adjustment records the
    triple before and after (PR-4), and a mutable rating makes "before"
    something the caller has to remember to copy.
    """

    value: float = INITIAL_RATING
    deviation: float = INITIAL_DEVIATION
    volatility: float = INITIAL_VOLATILITY

    def __post_init__(self) -> None:
        # Not rating-system rules — no input this platform can produce
        # reaches them. They catch a corrupted row or a provider returning
        # a default it should not have, at the point where the number is
        # still traceable to where it came from.
        if self.deviation <= 0:
            raise ValueError("rating deviation must be positive")
        if self.volatility <= 0:
            raise ValueError("volatility must be positive")

    @classmethod
    def initial(cls) -> "Glicko2Rating":
        """What a player who has never played this key rates at.

        A named constructor rather than relying on the field defaults, so
        that "this player has no rating yet" reads as an intention at the
        call site instead of as an empty constructor call.
        """
        return cls()


@dataclass(frozen=True, slots=True)
class MatchOutcomeScore:
    """One player's result as Glicko-2 scores it.

    A tiny type rather than a bare `float`, because the three legal values
    are the whole domain and a function taking `float` invites 0.75 to be
    passed to it.
    """

    value: float

    @classmethod
    def win(cls) -> "MatchOutcomeScore":
        return cls(1.0)

    @classmethod
    def draw(cls) -> "MatchOutcomeScore":
        return cls(0.5)

    @classmethod
    def loss(cls) -> "MatchOutcomeScore":
        return cls(0.0)

    def inverted(self) -> "MatchOutcomeScore":
        """The opponent's score for the same game.

        Here rather than at the call site so a rating update cannot award
        both players a win by computing the second score independently.
        """
        return MatchOutcomeScore(1.0 - self.value)


@dataclass(frozen=True, slots=True)
class GameResult:
    """One game, from the rated player's point of view.

    A pair rather than two parallel sequences, so an opponent cannot be
    silently matched with the wrong score by a caller that built two lists.
    """

    opponent: Glicko2Rating
    score: MatchOutcomeScore


def inflated(rating: Glicko2Rating, *, elapsed_seconds: float) -> Glicko2Rating:
    """`rating` with its deviation grown for a period of inactivity — §7.4.

    Step 6 of the paper, applied with a fractional period count instead of
    once per elapsed period, and capped at `INITIAL_DEVIATION`.

    Returns the rating **unchanged** for a non-positive duration, which is
    the correct answer for two real cases: a player's first match in a key
    (there is no previous one) and a pair of matches completing in the same
    instant. Neither is an error, so neither raises.

    The value and the volatility are untouched. SPEC-RATING N-5 is explicit
    that only *uncertainty* grows with absence — a rating that decayed
    towards the mean would be the platform taking points from somebody for
    not playing, which is a different product decision nobody has made.
    """
    if elapsed_seconds <= 0:
        return rating

    elapsed_periods = elapsed_seconds / _SECONDS_PER_DAY / RATING_PERIOD_DAYS

    phi = _to_phi(rating.deviation)
    inflated_phi = math.sqrt(phi * phi + rating.volatility * rating.volatility * elapsed_periods)

    return Glicko2Rating(
        value=rating.value,
        deviation=min(_to_deviation(inflated_phi), INITIAL_DEVIATION),
        volatility=rating.volatility,
    )


def expected_score(player: Glicko2Rating, opponent: Glicko2Rating) -> float:
    """The probability `player` scores against `opponent`, in [0, 1].

    Published because PR-4 requires an adjustment to record the inputs that
    produced it, and the expected score is the one input a player actually
    asks about: "why did I only gain two points for beating them?"

    It is the paper's `E(μ, μⱼ, φⱼ)`, computed on the internal scale and
    returned as a plain probability.
    """
    mu = _to_mu(player.value)
    opponent_mu = _to_mu(opponent.value)
    opponent_phi = _to_phi(opponent.deviation)
    return _expectation(mu, opponent_mu, opponent_phi)


def rated(player: Glicko2Rating, results: Sequence[GameResult]) -> Glicko2Rating:
    """`player` after the games in `results`.

    Steps 3 through 8 of the paper. **The caller is responsible for
    inflation** — `inflated` runs first, on the stored rating, because "how
    long has this player been away" is a fact about the world and this
    function is arithmetic.

    A sequence rather than one opponent, even though this platform rates one
    match at a time (ADR-001). Two reasons, and the second decided it:

    - It is what the paper specifies. Steps 3 and 4 are sums over the games
      of a period, and the single-game case is that sum with one term — so
      the general form is not extra generality, it is the algorithm.
    - **Glickman's published worked example uses three opponents.** A
      single-opponent signature could not run it, and that example is the
      only test that proves this is Glicko-2 rather than something that
      merely behaves plausibly.

    An empty sequence returns step 6's answer for a player who did not
    compete. On this platform it cannot happen — an update exists because a
    match completed — and it is handled rather than raised so the function
    is total.

    Opponents' ratings are used and never returned: rating the opponent is a
    second call with the roles swapped, which is what
    `MatchOutcomeScore.inverted` exists for. Returning both would make this
    function know it is being used for a two-player game, and Glicko-2 does
    not.
    """
    if not results:
        return rating_after_idle_period(player)

    mu = _to_mu(player.value)
    phi = _to_phi(player.deviation)

    # Steps 3 and 4: sums over the period's games. One term here, but the
    # loop is the algorithm rather than a generalisation of it.
    variance_terms = 0.0
    improvement_terms = 0.0
    for result in results:
        opponent_mu = _to_mu(result.opponent.value)
        opponent_phi = _to_phi(result.opponent.deviation)
        g_opponent = _g(opponent_phi)
        expectation = _expectation(mu, opponent_mu, opponent_phi)

        variance_terms += g_opponent * g_opponent * expectation * (1.0 - expectation)
        improvement_terms += g_opponent * (result.score.value - expectation)

    variance = 1.0 / variance_terms
    delta = variance * improvement_terms

    # Step 5: the new volatility. The only step without a closed form.
    new_volatility = _solved_volatility(
        phi=phi, variance=variance, delta=delta, volatility=player.volatility
    )

    # Step 6: pre-rating-period deviation. Here this absorbs *this* match's
    # own period rather than an absence, which `inflated` has already
    # handled — see this module's docstring.
    pre_phi = math.sqrt(phi * phi + new_volatility * new_volatility)

    # Step 7: the new deviation and the new rating, on the internal scale.
    new_phi = 1.0 / math.sqrt(1.0 / (pre_phi * pre_phi) + 1.0 / variance)
    new_mu = mu + new_phi * new_phi * improvement_terms

    # Step 8: back to the scale a player reads.
    return Glicko2Rating(
        value=_to_rating(new_mu),
        deviation=min(_to_deviation(new_phi), INITIAL_DEVIATION),
        volatility=new_volatility,
    )


def rating_after_idle_period(player: Glicko2Rating) -> Glicko2Rating:
    """Step 6 alone — a player who did not compete in a period.

    Their value and volatility stand and only their uncertainty grows.
    Named and kept separate because on this platform it is reached only
    through `rated([])`, which cannot happen: an update exists because a
    match completed. Absence is handled by `inflated` instead, from elapsed
    time — see this module's docstring.
    """
    phi = _to_phi(player.deviation)
    grown = math.sqrt(phi * phi + player.volatility * player.volatility)
    return Glicko2Rating(
        value=player.value,
        deviation=min(_to_deviation(grown), INITIAL_DEVIATION),
        volatility=player.volatility,
    )


def _solved_volatility(*, phi: float, variance: float, delta: float, volatility: float) -> float:
    """Step 5 — the Illinois algorithm, as the paper specifies it.

    A regula-falsi variant rather than bisection or Newton, and the choice
    is not interchangeable: Glickman's worked example pins the output to six
    decimal places, and a different solver reaches a different last digit —
    which would make the one test that proves this is Glicko-2 unable to
    prove it.
    """
    delta_squared = delta * delta
    phi_squared = phi * phi
    tau_squared = SYSTEM_CONSTANT_TAU * SYSTEM_CONSTANT_TAU
    alpha = math.log(volatility * volatility)

    def objective(x: float) -> float:
        exp_x = math.exp(x)
        numerator = exp_x * (delta_squared - phi_squared - variance - exp_x)
        denominator = 2.0 * (phi_squared + variance + exp_x) ** 2
        return numerator / denominator - (x - alpha) / tau_squared

    # The initial bracket. `a` is always a valid lower bound; `b` is chosen
    # differently depending on whether the observed change exceeds what the
    # current uncertainty explains — the paper's own case split.
    a = alpha
    if delta_squared > phi_squared + variance:
        b = math.log(delta_squared - phi_squared - variance)
    else:
        # Walk down until the objective turns negative. Bounded by the same
        # iteration cap, so a degenerate input cannot loop here either.
        k = 1
        while objective(alpha - k * SYSTEM_CONSTANT_TAU) < 0 and k <= _MAX_ITERATIONS:
            k += 1
        b = alpha - k * SYSTEM_CONSTANT_TAU

    objective_a = objective(a)
    objective_b = objective(b)

    for _ in range(_MAX_ITERATIONS):
        if abs(b - a) <= _CONVERGENCE_TOLERANCE:
            break

        c = a + (a - b) * objective_a / (objective_b - objective_a)
        objective_c = objective(c)

        if objective_c * objective_b <= 0:
            a, objective_a = b, objective_b
        else:
            # The Illinois modification: halve the retained endpoint's
            # value so the bracket cannot stagnate on one side, which is
            # exactly what plain regula falsi does here.
            objective_a /= 2.0

        b, objective_b = c, objective_c

    return math.exp(a / 2.0)


def _g(phi: float) -> float:
    """The paper's `g(φ)` — how much an opponent's uncertainty damps the
    weight of a result against them."""
    return 1.0 / math.sqrt(1.0 + 3.0 * phi * phi / (math.pi * math.pi))


def _expectation(mu: float, opponent_mu: float, opponent_phi: float) -> float:
    """The paper's `E(μ, μⱼ, φⱼ)`, on the internal scale."""
    return 1.0 / (1.0 + math.exp(-_g(opponent_phi) * (mu - opponent_mu)))


def _to_mu(value: float) -> float:
    return (value - INITIAL_RATING) / _GLICKO2_SCALE


def _to_phi(deviation: float) -> float:
    return deviation / _GLICKO2_SCALE


def _to_rating(mu: float) -> float:
    return mu * _GLICKO2_SCALE + INITIAL_RATING


def _to_deviation(phi: float) -> float:
    return phi * _GLICKO2_SCALE


__all__ = [
    "INITIAL_DEVIATION",
    "GameResult",
    "INITIAL_RATING",
    "INITIAL_VOLATILITY",
    "RATING_PERIOD_DAYS",
    "SYSTEM_CONSTANT_TAU",
    "Glicko2Rating",
    "MatchOutcomeScore",
    "expected_score",
    "inflated",
    "rated",
    "rating_after_idle_period",
]
