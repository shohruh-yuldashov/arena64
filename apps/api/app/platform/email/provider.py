"""Choosing the transport — A64-021.5 §12, and the decision it deferred.

One function, and it is the **only** place on this platform that decides
which `EmailProvider` a process holds. `auth`'s verification and reset mail
and `notifications`' tournament mail both come through here, which is what
makes "one sender identity, one credential, one retry story" a property of
the code rather than a note in a document.

## The vendor is Resend

A64-021.5 built the whole channel and stopped short of an adapter, because
choosing a provider is a billing and deliverability decision that a codebase
must not make on its own. It has been made: Resend, sending from
`no-reply@arena64.gg` on a domain already verified with them.

So the branch below is the whole of it. `RESEND_API_KEY` present means a
process can send; absent means it cannot, and `ConsoleEmailProvider` —
which **refuses to construct in a production-like environment** — makes that
a boot failure rather than a platform that silently sends nobody anything.

## Why the key is the switch, and not a second flag

`can_deliver_email` answers the same question from the same value. A boolean
saying "email works" that could disagree with whether a credential exists is
exactly the settings-screen lie §26 forbids: a player would be offered a
switch that turns on a channel with nothing behind it.
"""

import logging

from app.config.environment import Environment
from app.config.settings import EmailSettings
from app.platform.email.console import ConsoleEmailProvider
from app.platform.email.message import EmailProvider
from app.platform.email.resend import ResendEmailProvider

logger = logging.getLogger(__name__)


def build_email_provider(environment: Environment, settings: EmailSettings) -> EmailProvider:
    """The transport this process sends through.

    Takes the email settings rather than the whole `Settings`, so the choice
    cannot come to depend on something unrelated to transport — and so a
    caller can see from the signature that nothing here reads a database, a
    feature flag or a user.

    Built **once per process**: `ResendEmailProvider` owns an HTTP connection
    pool, and one per message would open a TLS connection per email.
    """
    key = settings.resend_api_key
    if key is None:
        # `ConsoleEmailProvider` raises here in a production-like tier, which
        # is deliberate and is the point: a deploy that forgot the credential
        # fails visibly and rolls back, rather than accepting registrations
        # nobody can verify (DI-06).
        return ConsoleEmailProvider(environment)

    logger.info(
        "email_transport_ready",
        # The sender and nothing else. Never the key, and never a length or a
        # prefix of it — both are information about a credential.
        extra={"transport": "resend", "from_address": settings.from_address},
    )
    return ResendEmailProvider(
        api_key=key.get_secret_value(),
        from_address=settings.from_address,
        from_name=settings.from_name,
    )


def can_deliver_email(settings: EmailSettings) -> bool:
    """Whether this process can actually put an email in somebody's inbox.

    The **same value** `build_email_provider` branches on, read by the
    composition root to build `ChannelAvailability`. One question, one
    source: a settings screen and a delivery worker that could disagree about
    whether email works is the failure both §5 and §26 name.

    `ConsoleEmailProvider` writing to a log is not delivery, and this returns
    `False` for it in every environment including a developer's. That is
    stricter than it needs to be for local convenience and is the honest
    answer — a developer exercising the flow sets a Resend key (their test
    key is enough), and nobody is ever shown a switch that does nothing.
    """
    return settings.resend_api_key is not None


__all__ = ["build_email_provider", "can_deliver_email"]
