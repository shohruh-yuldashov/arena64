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
from typing import Final

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


#: `apps/api`, derived from this file rather than from a working directory.
#:
#: Two levels up from `app/config/environment.py`. A relative path here would
#: make every command's behaviour depend on where it was typed, which is the
#: one thing a configuration loader must not do.
_API_ROOT: Final = Path(__file__).resolve().parents[2]


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

    **Absolute, and derived from this file's own location.** The path does
    not depend on where a command was run from, so `uv run` in `apps/api`,
    the API's startup command, an operator module and an invocation from the
    repository root all read the same file. That is deliberate and is
    verified by `tests/unit/test_settings.py`.
    """
    if environment is Environment.LOCAL:
        return _API_ROOT / ".env.local"
    return None


def describe_env_file(environment: Environment) -> str:
    """One line saying which file was read, for a startup log and an
    operator command.

    A missing env file is otherwise **completely silent**: the process starts
    on code defaults and nothing says the configuration a developer wrote was
    never opened. That silence is what turned a file named `.env` into an
    afternoon — the platform behaved exactly as if it had no configuration,
    because it had none.

    So the line names the file, says whether it exists, and — the part that
    would have ended it immediately — says when a differently-named file is
    sitting beside it.
    """
    path = env_file_for(environment)
    if path is None:
        return f"{environment.value}: no env file is read; configuration comes from the environment"
    if path.exists():
        return f"{environment.value}: read {path}"

    stray = _API_ROOT / ".env"
    if stray.exists():
        return (
            f"{environment.value}: {path} does not exist and is NOT being read — "
            f"but {stray} does. This platform reads .env.local; rename it"
        )
    return f"{environment.value}: {path} does not exist; running on code defaults"
