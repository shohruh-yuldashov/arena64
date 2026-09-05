"""Operator commands for the rate limiter — A64-020.6.

    python -m app.operator.rate_limits show
    python -m app.operator.rate_limits clear --dry-run
    python -m app.operator.rate_limits clear
    python -m app.operator.rate_limits clear --rule login_ip --rule register_ip

Two commands: report the live configuration, and drop the buckets it has
filled. See `app/operator/__init__.py` for why this is a process profile
rather than an `/api/v1/admin` route — it matters here as much as anywhere,
because an endpoint that could clear a rate-limit bucket is an endpoint that
removes the rate limit.

## What `clear` deletes, and what it cannot

**Only keys this platform's limiter owns** — `rl:v1:<rule>:*`, one pattern
per declared rule. `show` prints the current list; today it is seventeen
rules across `auth`, `profiles`, `matchmaking`, `friends`, `avatars` and
`notifications`.

The patterns are **derived from the policy registries**, not typed here. A
`build_rules` module is the one place a rule name exists, so a rule renamed
there is renamed here and a rule added there is cleared here — a hardcoded
list would silently stop covering the newest limit, which is the one an
operator is most likely to be fighting. It also stops covering rules that
were never in the list to begin with: reading `auth` alone left the
`profiles` buckets behind, which is why `_POLICY_REGISTRIES` exists.

Never `FLUSHALL` and never `FLUSHDB`. The `limits` role is its own Redis
instance today (AD-03), but "today" is not a safety property: a deployment
that consolidated two roles onto one instance would turn a flush into the
loss of every live match position on the platform. `SCAN` + `UNLINK` over a
known prefix cannot do that however the instances are arranged.

`SCAN` rather than `KEYS`, because `KEYS` is O(n) over the entire keyspace
and blocks the server for the duration — on an instance with real traffic
that is an outage, and this command exists to *end* one.

## When to use it

Development, and one production case.

In development the limits are real production values and a test suite that
registers an account per run exhausts the hourly budget in three runs. The
alternatives are worse: waiting out `Retry-After` is not a workflow,
and turning the limiter off means the suite never exercises the code that
ships. Clearing the buckets keeps the limiter in the request path and
resets the counter, which is the only one of the three that is honest.

In production it is the answer to a limit that was configured far too low
and has locked out real users: lower the number in the environment, restart,
and clear the buckets the wrong value filled. The alternative is telling
everybody to wait an hour.

**It is not a way to bypass a limit for one caller.** The scope is a rule,
never a subject: `--rule login_ip` clears every host's login bucket, and
there is deliberately no `--ip` or `--email`. A per-subject exemption is an
authorization decision, and this file is not where authorization gets
invented (see `app/operator/__init__.py`).

Subjects are hashed into the key (`subject_digest`), so targeting one would
mean re-deriving a digest from a plaintext address — this command never
holds one, which is why the operator log below can safely report counts.
"""

import argparse
import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass

from redis.asyncio import Redis

from app.common.logging import configure_logging
from app.config.settings import Settings, get_settings
from app.core.rate_limiting import KEY_PREFIX, KEY_VERSION, RateLimitProfile, scaled
from app.modules.analytics.presentation.rate_limits import (
    build_rules as analytics_rules,
)
from app.modules.auth.presentation.rate_limits import build_rules as auth_rules
from app.modules.avatars.presentation.rate_limits import build_rules as avatar_rules
from app.modules.friends.presentation.rate_limits import build_rules as friends_rules
from app.modules.matchmaking.presentation.rate_limits import build_rules as matchmaking_rules
from app.modules.notifications.presentation.rate_limits import build_rules as notification_rules
from app.modules.profiles.presentation.rate_limits import build_rules as profile_rules
from app.modules.tournament.presentation.rate_limits import (
    build_rules as tournament_rules,
)

logger = logging.getLogger(__name__)

#: How many keys one `SCAN` asks for. A hint rather than a guarantee — Redis
#: may return more or fewer — and it is deliberately not one: a cursor walk
#: at `count=1` over a large keyspace is thousands of round trips.
_SCAN_COUNT = 500


#: Every module that declares rate-limit policy, in the order they were
#: added. The *mechanism* is the platform's and the *policy* is each
#: module's, so there is no single registry to read — and a command that
#: knew about only one of them would leave the others' buckets behind. That
#: is not hypothetical: the first version of this file read `auth` alone and
#: its dry run reported three of the four buckets that existed.
#:
#: A module that adds a policy module must be added here. The alternative —
#: discovering them by import scanning — trades a one-line edit for a
#: mechanism that fails silently when a package is renamed.
#:
#: **It failed silently anyway.** A64-021.2H found `friends` and `avatars`
#: missing: an operator clearing buckets during a notification diagnosis
#: could not clear `friend_request_send_user`, which is the one bucket the
#: notification flow actually consumes, and the command reported success
#: while leaving it untouched. That is the exact failure this comment
#: predicted, twice over, and a one-line edit is only reliable if something
#: fails when it is forgotten — so
#: `tests/unit/test_rate_limit_operator.py` now asserts this tuple covers
#: every module that declares policy.
_POLICY_REGISTRIES = (
    analytics_rules,
    auth_rules,
    profile_rules,
    matchmaking_rules,
    friends_rules,
    avatar_rules,
    notification_rules,
    tournament_rules,
)


def rule_names(settings: Settings) -> list[str]:
    """Every rate-limit rule name the platform defines, sorted.

    Read from the registries rather than listed, so this command cannot
    fall behind the rules it is meant to clear. Each `build_rules` is keyed
    by endpoint and each entry holds one or two rules; the names are what
    appear in a key.
    """
    return sorted(
        {
            rule.name
            for build_rules in _POLICY_REGISTRIES
            for rules in build_rules(settings.rate_limit).values()
            for rule in rules
        }
    )


def pattern_for(rule: str) -> str:
    """The `SCAN MATCH` pattern for one rule's buckets.

    Built from the same two constants `RateLimitSubject.key` uses, so the
    pattern and the key cannot disagree about the prefix or the version.
    """
    return f"{KEY_PREFIX}:{KEY_VERSION}:{rule}:*"


async def clear(*, rules: Sequence[str], dry_run: bool) -> dict[str, int]:
    """Deletes every bucket belonging to `rules`. Returns counts per rule.

    `UNLINK` rather than `DEL`: reclaiming the memory happens on a
    background thread, so a rule with many thousands of buckets does not
    block the instance while it is freed. It has existed since Redis 4.0,
    which is far below this platform's floor (Redis 8), so there is no
    fallback and an older server is a deployment error rather than a case
    to tolerate.
    """
    settings = get_settings()
    client: Redis = Redis.from_url(settings.redis.limits_url.get_secret_value())
    try:
        return {rule: await _clear_rule(client, rule, dry_run=dry_run) for rule in rules}
    finally:
        await client.aclose()


async def _clear_rule(client: Redis, rule: str, *, dry_run: bool) -> int:
    """One rule's buckets, deleted in batches as the cursor walks.

    Deleted per page rather than collected and deleted at the end: a
    keyspace large enough to matter is a list large enough to not want in
    memory, and a partial run that deleted what it saw is a better outcome
    than one that held everything and failed at the end.
    """
    cleared = 0
    cursor = 0
    while True:
        cursor, keys = await client.scan(cursor=cursor, match=pattern_for(rule), count=_SCAN_COUNT)
        if keys:
            cleared += len(keys)
            if not dry_run:
                await client.unlink(*keys)
        if cursor == 0:
            return cleared


@dataclass(frozen=True, slots=True)
class EffectiveRule:
    """One rule as this process actually enforces it — A64-021.6 §8.

    Carries **both** numbers, and that is the point: a developer asking why
    they can sign in a hundred times on a laptop and five times in
    production should be able to see the production figure and the
    multiplier that moved it, in one line, rather than reading two files and
    inferring a mapping.
    """

    name: str
    base_limit: int
    """As the module policy declares it. Always production's figure."""

    effective_limit: int
    """As this process enforces it. `base_limit × profile.multiplier`."""

    window_seconds: int


async def show() -> tuple[RateLimitProfile, list[EffectiveRule]]:
    """The profile in force, and every rule as it is actually enforced.

    Reported because the first question during an incident is "what is the
    limit actually set to here", and reading it from the running process's
    settings answers it for *this* deployment rather than for the file
    somebody has open.

    Since A64-021.6 that question has two halves — the declared figure and
    the environment's multiplier — and answering only the first would be
    the more misleading of the two.
    """
    settings = get_settings()
    profile = settings.rate_limit.profile
    effective = [
        EffectiveRule(
            name=rule.name,
            base_limit=rule.limit,
            effective_limit=scaled(rule, profile).limit,
            window_seconds=int(rule.window.total_seconds()),
        )
        for build_rules in _POLICY_REGISTRIES
        for group in build_rules(settings.rate_limit).values()
        for rule in group
    ]
    # By name, explicitly: `EffectiveRule` is not orderable and should not
    # be — a dataclass with four numeric fields that sorted by all of them
    # would order by whichever happened to come first.
    return profile, sorted(effective, key=lambda entry: entry.name)


def _parser(known: Sequence[str]) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.operator.rate_limits",
        description="Inspect and clear the authentication rate-limit buckets.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("show", help="Print the live limit configuration.")

    drop = commands.add_parser("clear", help="Delete rate-limit buckets.")
    drop.add_argument(
        "--rule",
        action="append",
        choices=list(known),
        help=(
            "Clear only this rule's buckets. Repeatable. "
            "Omit to clear every rule. A rule name not in this list is "
            "rejected rather than matched as a pattern."
        ),
    )
    drop.add_argument(
        "--dry-run",
        action="store_true",
        help="Count what would be deleted and delete nothing.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    settings = get_settings()
    configure_logging(level=settings.app.log_level, environment=settings.environment)

    known = rule_names(settings)
    arguments = _parser(known).parse_args(argv)

    if arguments.command == "show":
        profile, rules = asyncio.run(show())
        # The profile first, because it explains every number below it.
        print(  # noqa: T201 — an operator's terminal
            f"environment={settings.environment.value} "
            f"profile={profile.value} multiplier=x{profile.multiplier}"
        )
        if profile is not RateLimitProfile.PRODUCTION:
            print(  # noqa: T201
                "  (production figures are shown in brackets and are unchanged)"
            )
        for entry in rules:
            scaling = (
                "" if entry.base_limit == entry.effective_limit else f"  [prod {entry.base_limit}]"
            )
            print(  # noqa: T201
                f"{entry.name:28s} {entry.effective_limit:6d} "
                f"per {entry.window_seconds:6d}s{scaling}"
            )
        return 0

    # `--rule` is validated against the registry by argparse, so an unknown
    # name is a usage error rather than a pattern that matches nothing and
    # reports success.
    selected = arguments.rule or known
    cleared = asyncio.run(clear(rules=selected, dry_run=arguments.dry_run))

    total = sum(cleared.values())
    for rule in selected:
        print(f"{rule:28s} {cleared[rule]:6d}")  # noqa: T201 — an operator's terminal

    verb = "would clear" if arguments.dry_run else "cleared"
    print(f"{verb} {total} bucket(s) across {len(selected)} rule(s)")  # noqa: T201
    if total and not arguments.dry_run:
        # An operator log, because clearing a limit is a security-relevant
        # act and the next person to read the incident timeline needs it.
        # Counts only: a subject is a digest here and a plaintext address
        # never enters this process.
        logger.warning(
            "rate_limit_buckets_cleared",
            extra={"rules": ",".join(selected), "cleared": total},
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["clear", "main", "pattern_for", "rule_names", "show"]
