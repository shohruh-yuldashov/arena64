"""Which transport a process actually gets — A64-021.5 continuation §10.

Structural, and about the composition root rather than about any service.
The defect it exists for is the one A64-021.5 shipped with and the
continuation fixed: every part of the email channel worked, and the only
provider a process could build wrote to a log.

## Why this is four tests and not one per caller

`auth`'s verification mail, `auth`'s reset mail and `notifications`' worker
all resolve the **same** function. Asserting that once, plus asserting that
each caller reaches it, is the whole claim — a test per caller sending a
message through a stub would be three tests proving the same wiring.
"""

import pathlib

import pytest
from pydantic import SecretStr

from app.config.environment import Environment, env_file_for
from app.config.settings import AppSettings, EmailSettings, NotificationEmailSettings
from app.modules.auth.presentation.dependencies import get_email_provider
from app.modules.notifications.presentation.dependencies import email_channel_available
from app.platform.email import (
    ConsoleEmailProvider,
    ResendEmailProvider,
    build_email_provider,
    can_deliver_email,
)

CONFIGURED = EmailSettings(resend_api_key=SecretStr("re_test_key"))
UNCONFIGURED = EmailSettings()


class TestTheTransport:
    def test_a_configured_key_builds_resend(self) -> None:
        """§10.1. The production composition, asserted at its one branch."""
        provider = build_email_provider(Environment.PRODUCTION, CONFIGURED)

        assert isinstance(provider, ResendEmailProvider)

    def test_no_key_refuses_to_start_a_deployed_tier(self) -> None:
        """A deploy that forgot the credential fails **visibly**.

        `ConsoleEmailProvider`'s guard is what makes the missing-credential
        case a rolled-back deploy rather than a platform accepting
        registrations nobody can ever verify (DI-06).
        """
        with pytest.raises(ValueError, match="ConsoleEmailProvider"):
            build_email_provider(Environment.PRODUCTION, UNCONFIGURED)

    def test_no_key_locally_is_the_console(self) -> None:
        """A developer without a Resend key still runs the whole platform.

        What they do not get is delivery — see `TestAvailability`, which is
        what stops that being a settings-screen lie.
        """
        provider = build_email_provider(Environment.LOCAL, UNCONFIGURED)

        assert isinstance(provider, ConsoleEmailProvider)


class TestAuthUsesTheSameTransport:
    def test_verification_and_reset_resolve_the_production_provider(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """§10.7, §10.8. `auth` did not have to know Resend exists.

        `get_email_provider` is the one dependency both
        `EmailVerificationService` and `PasswordResetService` are constructed
        with, so proving it resolves to Resend proves both — and a test that
        sent a verification message through a stub would be asserting
        `auth`'s unchanged behaviour, not this change.

        The settings object is built by hand rather than through
        `get_settings`, so this asserts the branch rather than whichever
        credential happens to be in the developer's environment.
        """

        class _Settings:
            environment = Environment.PRODUCTION
            email = CONFIGURED

        provider = get_email_provider(_Settings())  # type: ignore[arg-type]

        assert isinstance(provider, ResendEmailProvider)


class TestTheEnvironmentVariableNames:
    """The names an operator actually types.

    **This class exists because of a bug the other tests could not catch.**
    `EmailSettings` carries an `EMAIL_` prefix, so `resend_api_key` read
    `EMAIL_RESEND_API_KEY` — while every test above constructed the settings
    object directly and passed. An operator following Resend's own
    documentation would have set `RESEND_API_KEY`, seen no error, and run a
    deployment that silently could not send.

    Constructing a settings class is not the same as configuring one, and
    these two read the environment.
    """

    def test_the_resend_key_is_read_from_resend_api_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("RESEND_API_KEY", "re_from_the_environment")
        monkeypatch.delenv("EMAIL_RESEND_API_KEY", raising=False)

        settings = EmailSettings()

        assert settings.resend_api_key is not None
        assert settings.resend_api_key.get_secret_value() == "re_from_the_environment"

    def test_the_origin_is_read_from_public_app_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PUBLIC_APP_URL", "https://arena64.gg")
        monkeypatch.delenv("APP_PUBLIC_URL", raising=False)

        assert AppSettings().public_url == "https://arena64.gg"


class TestTheEnvFile:
    """Loading configuration from `.env.local`, which had never worked.

    **These are the tests the defect got past.** Every other test here
    constructs a settings object directly, and the failure was in the file
    source: `pydantic-settings` does not filter a dotenv file by
    `env_prefix`, so with `extra="forbid"` a file holding two sections'
    keys made both sections refuse to construct. `.env.example`'s first line
    says "Copy to .env.local", and doing so crashed the process.

    It surfaced as a Resend credential that would not load. It was never
    about Resend.
    """

    def test_a_realistic_file_loads_every_section(self, tmp_path: pathlib.Path) -> None:
        """One file, four sections, no crash — and the values arrive.

        The mix is the point: an `APP_`-prefixed key, an `EMAIL_`-prefixed
        key, a `NOTIFICATION_EMAIL_`-prefixed key, and the two whose names
        are fixed by an external contract rather than by a prefix. Before the
        fix, any two of these together were a `ValidationError`.
        """
        env_file = tmp_path / ".env.local"
        env_file.write_text(
            "APP_LOG_LEVEL=DEBUG\n"
            "RESEND_API_KEY=re_only_a_test_value\n"
            "EMAIL_FROM_ADDRESS=no-reply@arena64.gg\n"
            "EMAIL_FROM_NAME=Arena64\n"
            "PUBLIC_APP_URL=https://arena64.gg\n"
            "NOTIFICATION_EMAIL_ENABLED=false\n"
        )

        app = AppSettings(_env_file=env_file)  # type: ignore[call-arg]
        email = EmailSettings(_env_file=env_file)  # type: ignore[call-arg]
        notifications = NotificationEmailSettings(_env_file=env_file)  # type: ignore[call-arg]

        assert app.log_level == "DEBUG"
        assert app.public_url == "https://arena64.gg"
        assert email.from_address == "no-reply@arena64.gg"
        assert email.from_name == "Arena64"
        assert notifications.enabled is False
        # **The secret is never asserted, printed or compared.** That it was
        # read is the whole claim; what it is belongs to the operator.
        assert email.resend_api_key is not None
        assert can_deliver_email(email) is True

    def test_a_section_ignores_another_section_key(self, tmp_path: pathlib.Path) -> None:
        """The narrowing, stated directly.

        `EmailSettings` must not see `APP_LOG_LEVEL` — that is what
        `extra="forbid"` would reject and what the filter removes. The guard
        itself is unchanged for process environment variables, where a typo
        in a deployed tier is still a refusal to start.
        """
        env_file = tmp_path / ".env.local"
        env_file.write_text("APP_LOG_LEVEL=DEBUG\nPOSTGRES_POOL_SIZE=7\n")

        assert EmailSettings(_env_file=env_file).from_name == "Arena64"  # type: ignore[call-arg]

    def test_the_env_file_path_does_not_depend_on_the_working_directory(self) -> None:
        """`.env.local`, always, wherever a command was typed.

        `uv run` from `apps/api`, the API's startup command, an operator
        module and an invocation from the repository root must read one file.
        The path is derived from the settings module's own location, so this
        asserts the shape rather than re-running four commands.
        """
        path = env_file_for(Environment.LOCAL)

        assert path is not None
        assert path.is_absolute()
        assert path.name == ".env.local"
        assert path.parent.name == "api"

    def test_a_deployed_tier_reads_no_file_at_all(self) -> None:
        """dependency-injection.md §2.2: secrets never come from a file
        layer. `RESEND_API_KEY` in production is a process environment
        variable from the secret manager, and nothing else."""
        assert env_file_for(Environment.PRODUCTION) is None
        assert env_file_for(Environment.TEST) is None


class TestAvailability:
    def test_email_is_unavailable_without_a_credential(self) -> None:
        """§10.2, and the lie this prevents.

        A player must not be offered an email switch in a deployment that
        cannot send one. The credential is the same value the provider
        branch reads, so a settings screen and a delivery worker cannot
        disagree about whether email works.
        """
        assert can_deliver_email(UNCONFIGURED) is False
        assert can_deliver_email(CONFIGURED) is True

    @pytest.mark.parametrize(
        ("email", "enabled", "expected"),
        [
            (CONFIGURED, True, True),
            (CONFIGURED, False, False),
            (UNCONFIGURED, True, False),
            (UNCONFIGURED, False, False),
        ],
    )
    def test_both_conditions_are_required(
        self, email: EmailSettings, enabled: bool, expected: bool
    ) -> None:
        """A transport **and** the kill switch.

        The two answer different questions — *can this process send at all*
        and *should it send notifications* — and the table is here because
        the interesting cases are the mixed ones: a credential with the
        channel switched off is an operator stopping notification mail
        without withdrawing the credential verification mail depends on.
        """

        class _Settings:
            def __init__(self) -> None:
                self.email = email
                self.notification_email = NotificationEmailSettings(enabled=enabled)

        assert email_channel_available(_Settings()) is expected  # type: ignore[arg-type]
