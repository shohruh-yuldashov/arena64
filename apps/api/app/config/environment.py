"""The environment loader.

Resolves *which* environment the process is running under and *which* env
file layer it should read on top of code defaults, per the layering in
dependency-injection.md §2.2:

    code defaults -> local env file -> process environment -> secret manager

This module owns only the first arrow (which file, if any, sits above the
defaults). `app.config.settings.Settings` (a pydantic-settings `BaseSettings`)
owns the rest of the chain natively: environment variables always take
precedence over an env file, and secrets are injected as real environment
variables in every deployed tier — see dependency-injection.md §2.4.
"""

import os
from enum import StrEnum
from pathlib import Path

_ENVIRONMENT_VARIABLE = "ENVIRONMENT"


class Environment(StrEnum):
    """The five environments of dependency-injection.md §2.3.

    Each has distinguishing behaviour documented there; this type is what
    lets Settings and logging branch on it instead of comparing strings.
    """

    LOCAL = "local"
    TEST = "test"
    CI = "ci"
    STAGING = "staging"
    PRODUCTION = "production"

    @property
    def is_local(self) -> bool:
        return self is Environment.LOCAL

    @property
    def is_test(self) -> bool:
        # `test` and `ci` both mean "no real infrastructure assumed absent
        # explicit opt-in" for anything gated on this flag — dependency-
        # injection.md §2.3 distinguishes them only by which suites run,
        # not by this behaviour.
        return self in (Environment.TEST, Environment.CI)

    @property
    def is_production_like(self) -> bool:
        return self in (Environment.STAGING, Environment.PRODUCTION)

    @property
    def uses_human_readable_logs(self) -> bool:
        # dependency-injection.md §2.3: `local` gets human-readable logs;
        # every other tier gets structured JSON that a log pipeline parses.
        return self is Environment.LOCAL


def current_environment() -> Environment:
    """Read `ENVIRONMENT` from the process environment.

    Defaults to `local` — a developer who has not set anything gets the
    most permissive, most observable tier, never a silent guess at
    `production` behaviour.
    """
    raw = os.environ.get(_ENVIRONMENT_VARIABLE, Environment.LOCAL.value)
    try:
        return Environment(raw)
    except ValueError as exc:
        valid = ", ".join(member.value for member in Environment)
        raise ValueError(f"{_ENVIRONMENT_VARIABLE}={raw!r} is not one of: {valid}") from exc


def env_file_for(environment: Environment) -> Path | None:
    """The local env file layer for this environment, if one applies.

    Only `local` reads a file from disk (`.env.local`, gitignored, a
    developer's own machine). `test` sets what little configuration it
    needs directly in `tests/conftest.py`. Every other tier is configured
    entirely by real process environment variables and the secret manager —
    dependency-injection.md §2.2 is explicit that secrets are never sourced
    from a file layer.
    """
    if environment is Environment.LOCAL:
        return Path(__file__).resolve().parents[2] / ".env.local"
    return None
