"""`tournament`'s rate-limit policy — A64-026.4 §43.6.

The mechanism is the platform's (`app/core/rate_limiting.py`,
`app/database/rate_limiter.py`, `app/api/rate_limiting.py`); the *policy* is
this module's, exactly as `auth.presentation.rate_limits` and
`profiles.presentation.rate_limits` are theirs. Nothing in the shared
mechanism changed to accommodate it — which is the third time that
prediction has held.

The numbers live on `RateLimitSettings` rather than here, for the reason
that class gives: a limit that can only be changed by a deploy cannot be
tightened during an incident.

## Why these three reads are limited and the rest of the module is not

They became **anonymous** in A64-026.4. Before that every tournament read
sat behind a token, and a token is a subject the platform can already count
against — an abusive caller is an account, and an account is revocable.

An anonymous endpoint has no such subject, so the only dimension left is the
network address. That is a weaker control and its budget is looser, exactly
as `profiles`' anonymous profile read records: an office, a university or a
mobile carrier is one bucket, and a limit tight enough to stop a scraper
would lock out a lecture hall.

What an unbounded caller could accumulate is the argument for having the
limit at all:

    lobby       a page at a time, the whole tournament directory — which is
                the enumeration `profiles` guards its search against, in the
                one shape this module has
    detail      one tournament per request, by UUID. Guessing is worthless
                (§43.2's 404), but a caller walking the lobby's cursors has
                every id and can fetch them all
    bracket     the most expensive read on the module — a whole field's
                nodes and every entrant's public profile in one response

Registration and withdrawal are **not** limited here. Both are behind
`VerifiedUser`, both are already bounded by the tournament's own capacity
and lifecycle rules, and a player who enters and leaves a tournament
repeatedly is a product problem the domain answers, not a throughput one.

## One guard, not three

The three reads share a budget because they are one activity: nobody browses
a bracket without having browsed the lobby that led to it, and three
separate allowances would let a caller spend all of them. A single bucket is
also the one a person hitting the limit can understand.

It is IP-scoped, which is what lets it attach as a bare `Depends(...)` —
`resolve_subjects` needs no principal for it, and that is precisely what
makes an anonymous endpoint able to carry a limit at all.
"""

from collections.abc import Sequence
from datetime import timedelta

from app.api.rate_limiting import RateLimit
from app.config.settings import RateLimitSettings
from app.core.rate_limiting import RateLimitRule, RateLimitScope


def build_rules(
    settings: RateLimitSettings,
) -> dict[str, Sequence[RateLimitRule]]:
    """This module's policy, keyed by endpoint.

    A mapping rather than a tuple, and that shape is load-bearing:
    `app/operator/rate_limits.py` reads every module's `build_rules` to
    learn which buckets exist, and iterates the values. A module returning
    a bare sequence would break the one command an operator has for
    clearing a bucket during an incident.
    """
    return {
        "tournament_public_read": (
            RateLimitRule(
                name="tournament_public_read_ip",
                scope=RateLimitScope.IP,
                limit=settings.tournament_read_ip_limit,
                window=timedelta(seconds=settings.tournament_read_window_seconds),
            ),
        ),
    }


def _guard(endpoint: str) -> RateLimit:
    """One endpoint's dependency, as `profiles` builds one.

    The guard captures a *lookup*, not the rules — the settings arrive per
    request, so a limit can be tightened without a deploy. The lookup raises
    `KeyError` at startup for a name `build_rules` does not define, which is
    the failure worth having: a typo would otherwise produce an endpoint
    with no limits and no error.
    """

    def rules_for(settings: RateLimitSettings) -> Sequence[RateLimitRule]:
        return build_rules(settings)[endpoint]

    return RateLimit(endpoint, rules_for)


#: The module's one guard. IP-scoped, which is what lets it attach as a bare
#: `Depends(...)` — `resolve_subjects` needs no principal for it, and that is
#: precisely what makes an anonymous endpoint able to carry a limit.
TOURNAMENT_READ_RATE_LIMIT = _guard("tournament_public_read")
