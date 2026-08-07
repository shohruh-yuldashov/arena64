"""`ConsoleEmailProvider` — the only `EmailProvider` this platform ships.

Writes the message to the application log instead of sending it, so the
whole verification flow is exercisable end to end on a developer machine
with no vendor account, no SMTP server and no network.

## This provider logs the verification link. On purpose.

Every other component on this platform is forbidden from logging a
credential (services.md §8.5), and this one exists to do exactly that.
The contradiction is only apparent: the link is *the message*, and a
provider whose transport is the log necessarily writes the message to the
log. A console provider that redacted the token would print an email
nobody can act on, which is the same as not having a development flow at
all.

What makes that safe is a guard rather than a convention: `__init__`
**refuses to construct in a production-like environment**. It is not
"don't configure this in production" written in a comment — it is an
exception at startup, which DI-06 makes a rolled-back deploy rather than
a silent credential leak into a log aggregator.

The log record is `WARNING`, not `INFO`, and that is deliberate too: a
deployed tier that somehow reached this code should produce a line that
an alert can match on, not one that blends into request logging.
"""

import logging

from app.config.environment import Environment
from app.platform.email.message import EmailMessage

logger = logging.getLogger(__name__)


class ConsoleEmailProvider:
    """Logs messages rather than sending them. Development and test only."""

    def __init__(self, environment: Environment) -> None:
        if environment.is_production_like:
            # DI-06's enforcement point. A deployed tier wired to this
            # provider would (a) send nobody anything, so every
            # registration would stall unverifiably, and (b) write live
            # verification links into the log pipeline. Refusing to start
            # is a visible deploy failure; starting is a silent one.
            raise ValueError(
                f"ConsoleEmailProvider must not be used in {environment} — it "
                "sends nothing and writes verification links to the log. "
                "Configure a real EmailProvider for deployed tiers."
            )
        self._environment = environment

    async def send(self, message: EmailMessage) -> None:
        """Writes the message where a developer will see it.

        The **text** part only, even when a caller supplied markup: a log
        line is a text transport, and dumping a rendered HTML document into
        one buries the sentence a developer is looking for. §17's rule that
        every transactional email carries both parts is about what reaches a
        mailbox, not about what a console prints.

        Never raises. The console transport has no failure mode worth
        modelling, and inventing one would make every caller handle an
        error that cannot happen here — while the caller that matters
        (`EmailVerificationService`) already handles the failures a real
        provider has.
        """
        logger.warning(
            "email_not_sent_console_provider\n  to:      %s\n  subject: %s\n%s",
            message.to,
            message.subject,
            message.text_body,
        )
