"""Outbound email — the port, the message, and nothing about SMTP.

`EmailProvider` is the seam. Everything above it composes messages;
everything below it is a vendor. A64-011.6's brief is explicit that no
SMTP provider is integrated yet, and the shape here is what makes adding
one a new class in `infrastructure/` rather than a change to any service.

## Why the port takes a message rather than fields

`send(message)` and not `send(to, subject, body)`. Adding a `reply_to`,
an attachment or a template id to a four-argument signature changes every
implementation and every call site; adding a field to `EmailMessage`
changes neither. Providers that cannot honour a field ignore it, which is
the correct behaviour for a transport detail — `ConsoleEmailProvider`
does not have a concept of a sender reputation and should not have to
pretend.

## Why `send` is async and returns nothing

Async because every real implementation is network I/O, and a
synchronous `send` in an async handler would block the event loop for the
duration of an SMTP conversation — the same reasoning that put Argon2 on
a worker thread, for a cost two orders of magnitude larger.

Returns `None` because there is nothing truthful to return. A provider
accepting a message means the *provider* accepted it, not that anyone
received it; delivery is asynchronous, out of this process, and reported
by webhook if at all. A `bool` here would be a lie that callers would
branch on.

## What this deliberately does not do

No retries, no queue, no outbox. A64-011.6 sends inline, which is
adequate for a console provider and is *not* adequate for a real one —
architecture.md AD-16's transactional outbox is where this belongs, and
the recommendation for A64-011.7 says so. What matters now is that the
port's shape does not prevent it: a queueing provider is just another
implementation.
"""

from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True, slots=True)
class EmailMessage:
    """One message, ready to hand to a provider.

    A plain frozen dataclass rather than a Pydantic model: nothing here
    crosses the wire to a client, and a Pydantic model is one keystroke
    from being a `response_model`. A message body containing a
    verification link is precisely the thing that must never be
    serialisable into an HTTP response.

    `text_body` and no HTML. A verification email that is plain text
    renders everywhere, cannot carry a tracking pixel, and cannot be the
    vector for the markup bugs that mail clients are famous for. HTML is a
    presentation upgrade for whoever adds templates, not a requirement of
    the flow.
    """

    to: str
    subject: str
    text_body: str = field(repr=False)
    """`repr=False` — the body of a verification message *contains the
    raw token*. It is the one place on the platform where that value
    legitimately exists in full, and a dataclass repr lands in tracebacks
    and error reporters (services.md §8.5)."""


class EmailProvider(Protocol):
    """Hands a composed message to a transport.

    A `Protocol`, not an ABC, so a test double satisfies it structurally
    and a vendor SDK wrapper does not inherit from anything.

    Implementations planned: SMTP, Resend, Amazon SES, Mailgun, SendGrid.
    Implemented today: `ConsoleEmailProvider`. The list is in the task
    brief and is recorded here rather than stubbed, for the reason
    `TokenType` records `REFRESH` in prose — an unimplemented class named
    `SesEmailProvider` reads as "this is wired up".
    """

    async def send(self, message: EmailMessage) -> None:
        """Delivers, or raises.

        Raises rather than returning a failure flag, because a caller that
        can meaningfully continue after a send failure is rare and should
        say so explicitly with a `try`. See `EmailVerificationService` for
        the one place that does, and why.
        """
        ...
