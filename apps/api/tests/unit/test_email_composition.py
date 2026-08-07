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

import pytest
from pydantic import SecretStr

from app.config.environment import Environment
from app.config.settings import EmailSettings, NotificationEmailSettings
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
