"""The rate-limit operator command — A64-020.6.

Three tests, and all three are about the same risk: this command deletes
Redis keys, so what it *cannot* match matters more than what it can.

Nothing here talks to a real Redis. `clear` is driven through a fake that
records every call, which is what lets the "never flushes" assertion be
about the command's behaviour rather than about a database that happened
to survive.
"""

from pathlib import Path
from typing import Any

from app.config.settings import RateLimitSettings, Settings, get_settings
from app.core.rate_limiting import KEY_PREFIX, KEY_VERSION, RateLimitRule, RateLimitSubject
from app.modules.auth.presentation.rate_limits import build_rules
from app.operator.rate_limits import (
    _POLICY_REGISTRIES,
    _clear_rule,
    pattern_for,
    rule_names,
)


class _FakeRedis:
    """A keyspace and a ledger of what was asked of it.

    `scan` honours the glob so a wrong pattern shows up as a wrong result
    rather than as a passing test — matching everything would otherwise be
    indistinguishable from matching correctly.
    """

    def __init__(self, keys: dict[str, str]) -> None:
        self.keys = dict(keys)
        self.unlinked: list[str] = []
        self.calls: list[str] = []

    async def scan(self, *, cursor: int, match: str, count: int) -> tuple[int, list[str]]:
        self.calls.append(f"scan {match}")
        prefix, _, suffix = match.partition("*")
        assert suffix == "", "these patterns are prefix globs; a suffix would need real matching"
        return 0, [key for key in self.keys if key.startswith(prefix)]

    async def unlink(self, *keys: str) -> int:
        self.calls.append("unlink")
        self.unlinked.extend(keys)
        for key in keys:
            self.keys.pop(key, None)
        return len(keys)

    def __getattr__(self, name: str) -> Any:
        # Any method this command does not use — `flushall`, `flushdb` —
        # records itself and fails the assertion below rather than silently
        # doing nothing.
        def recorder(*_args: object, **_kwargs: object) -> None:
            self.calls.append(name)

        return recorder


def _settings() -> Settings:
    return get_settings()


class TestThePatterns:
    def test_every_pattern_is_confined_to_the_limiter_keyspace(self) -> None:
        """The one property that makes this command safe to run against a
        shared instance: a pattern that lost its prefix would match keys
        belonging to `live` or `cache` — every position of every match in
        progress, deleted by a command whose name says "rate limits"."""
        for rule in rule_names(_settings()):
            pattern = pattern_for(rule)

            assert pattern.startswith(f"{KEY_PREFIX}:{KEY_VERSION}:")
            assert pattern.endswith(":*")
            assert "*" not in pattern[:-1], "a glob anywhere but the end widens the match"

    def test_a_pattern_matches_the_key_the_limiter_actually_writes(self) -> None:
        """Asserted against a **real** key from `RateLimitSubject`, not a
        hand-written string. The two are built from the same constants, and
        this is what notices if one of them changes and the other does not
        — a rename that made every pattern match nothing would otherwise
        present as "clear reports zero", which looks like success."""
        rule = build_rules(RateLimitSettings())["login"][0]
        key = RateLimitSubject(rule=rule, subject="198.51.100.7").key
        pattern = pattern_for(rule.name)

        assert key.startswith(pattern.removesuffix("*"))

    def test_the_rule_list_spans_every_module_that_declares_policy(self) -> None:
        """Policy is per module, so a command reading one registry leaves the
        others' buckets behind.

        **Discovered from the filesystem, not listed here** — A64-021.2H.
        The previous version of this test named three rules, one per module
        somebody remembered, and so it could not fail for a module nobody
        had listed. Two were missing: `friends` and `avatars`. An operator
        clearing buckets mid-incident was told "cleared" while
        `friend_request_send_user` — the one bucket a notification
        diagnosis actually consumes — was left untouched.

        So this walks `app/modules/*/presentation/rate_limits.py` and
        requires every one of them to contribute a rule. Adding a policy
        module now fails this test until `_POLICY_REGISTRIES` is updated,
        which is what the registry's own comment has promised since
        A64-020.6 and could not deliver.
        """
        modules = sorted(
            path.parents[1].name
            for path in (Path(__file__).resolve().parents[2] / "app" / "modules").glob(
                "*/presentation/rate_limits.py"
            )
        )
        assert modules, "no module declares rate-limit policy — the glob is wrong"

        covered = {registry.__module__.split(".")[2] for registry in _POLICY_REGISTRIES}
        assert covered == set(modules), (
            f"_POLICY_REGISTRIES covers {sorted(covered)}; "
            f"the modules that declare policy are {modules}"
        )

        # And the registry actually yields each module's rules, so a
        # registry entry that imported the wrong `build_rules` still fails.
        names = rule_names(_settings())
        for expected in (
            "login_ip",  # auth
            "privacy_update_user",  # profiles
            "matchmaking_queue_user",  # matchmaking
            "friend_request_send_user",  # friends
            "avatar_upload_user",  # avatars
        ):
            assert expected in names


class TestClearing:
    async def test_it_deletes_only_the_rule_it_was_asked_for(self) -> None:
        """A live match position sharing the instance must survive. AD-03
        gives `limits` its own Redis today, but a deployment that
        consolidated roles would make a careless pattern catastrophic."""
        rule = build_rules(RateLimitSettings())["login"][0]
        mine = RateLimitSubject(rule=rule, subject="198.51.100.7").key
        redis = _FakeRedis(
            {
                mine: "1",
                f"{KEY_PREFIX}:{KEY_VERSION}:register_ip:abc": "1",
                "match:019fe:position": "not mine",
                "presence:player:019fe": "not mine",
            }
        )

        cleared = await _clear_rule(redis, rule.name, dry_run=False)  # type: ignore[arg-type]

        assert cleared == 1
        assert redis.unlinked == [mine]
        assert "match:019fe:position" in redis.keys
        assert "presence:player:019fe" in redis.keys
        assert f"{KEY_PREFIX}:{KEY_VERSION}:register_ip:abc" in redis.keys

    async def test_a_dry_run_counts_and_writes_nothing(self) -> None:
        rule = build_rules(RateLimitSettings())["register"][0]
        key = RateLimitSubject(rule=rule, subject="198.51.100.7").key
        redis = _FakeRedis({key: "1"})

        cleared = await _clear_rule(redis, rule.name, dry_run=True)  # type: ignore[arg-type]

        assert cleared == 1
        assert redis.unlinked == []
        assert key in redis.keys

    async def test_it_never_reaches_for_a_flush(self) -> None:
        """`FLUSHALL` and `FLUSHDB` are the two calls that turn a
        rate-limit reset into data loss. The fake records any method it is
        asked for, so this fails if either is ever added."""
        rule: RateLimitRule = build_rules(RateLimitSettings())["login"][0]
        redis = _FakeRedis({RateLimitSubject(rule=rule, subject="203.0.113.9").key: "1"})

        await _clear_rule(redis, rule.name, dry_run=False)  # type: ignore[arg-type]

        assert not {call for call in redis.calls if "flush" in call.lower()}
        assert redis.calls == [f"scan {pattern_for(rule.name)}", "unlink"]
