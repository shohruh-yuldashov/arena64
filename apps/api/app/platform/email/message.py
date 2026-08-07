"""Outbound email — the port, the message, and nothing about SMTP.

## Why this lives in `platform` and not in `auth` — A64-021.5

It was `auth.application.email` from A64-011.6, when the only outbound mail
on this platform was a verification link. A64-021.5 gave it a second
consumer — `notifications`, sending a tournament confirmation — and a
transport that two bounded contexts share is not one context's domain.

The alternative was a second stack, which §1 of that brief forbids for the
obvious reason: two ways to send an email is two sender identities, two
retry stories and two places a credential is configured.

`platform` is where the outbox and the task scheduler already live, and the
rule that keeps it honest is `.importlinter`'s: **platform imports no
bounded context**. A message and a `send` cannot reach a user, a
notification or a session.

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

## Why `send` is async, and what it now returns

Async because every real implementation is network I/O, and a
synchronous `send` in an async handler would block the event loop for the
duration of an HTTP round trip — the same reasoning that put Argon2 on a
worker thread, for a cost two orders of magnitude larger.

A64-011.6 returned `None`, on the reasoning that *"a provider accepting a
message means the provider accepted it, not that anyone received it… a
`bool` here would be a lie that callers would branch on."* That is still
true and is why there is still no boolean.

What it returns instead is the **provider's own reference** for the
message, or `None` where a transport has none. That is not a delivery
claim: it is the string an operator types into a vendor's dashboard to
find out what happened to a message somebody says never arrived. A
platform that could not answer *"we sent it, here is their id for it"*
would have nothing to investigate with — and that answer is truthful in a
way `delivered: bool` never was.

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

    A64-011.6 shipped `text_body` and no HTML, on the reasoning that a
    verification email in plain text renders everywhere, cannot carry a
    tracking pixel, and cannot be the vector for the markup bugs mail
    clients are famous for. That reasoning still holds for a link somebody
    must click, and `html_body` is **additive** for the messages that are
    read rather than acted on — see below.
    """

    to: str
    subject: str
    text_body: str = field(repr=False)
    """`repr=False` — the body of a verification message *contains the
    raw token*. It is the one place on the platform where that value
    legitimately exists in full, and a dataclass repr lands in tracebacks
    and error reporters (services.md §8.5)."""

    html_body: str | None = field(default=None, repr=False)
    """The same message as markup, or `None` — A64-021.5 §17.

    Optional rather than required, and the two callers make opposite
    choices for the same reason. A verification message is a link somebody
    must click and is sent as **text only**: nothing about it is improved by
    markup, and every mail client renders a bare URL. A notification email
    is *read*, has a call to action, and ships both parts — §17 requires
    that a transactional email never be HTML-only, which is a property of
    the pair rather than of this field.

    `repr=False` for the reason above: a rendered body carries a display
    name, and a dataclass repr lands in tracebacks.

    A provider that cannot honour markup ignores it, which is the correct
    behaviour for a transport detail — `ConsoleEmailProvider` has no
    concept of a multipart message and should not have to pretend.
    """

    idempotency_key: str | None = None
    """A caller-chosen reference that makes a retry safe at the *provider*.

    `None` where a caller has nothing stable to key on. `auth`'s two
    messages are examples: a verification resend is a **new** message the
    player asked for, and keying it would make the second request silently
    return the first one's result.

    A notification email has one — its delivery row's identity — and the
    value of sending it is what it covers that this platform's own
    idempotency cannot. The delivery table stops a *second attempt* being
    started; nothing here can stop a first attempt that timed out after the
    provider had already accepted it. That message is already sent, and the
    retry is what would duplicate it.

    Not a secret and not a token: it is derived from an identifier the
    recipient already holds, so it may appear in a request header.
    """


class PermanentEmailFailure(Exception):
    """A provider rejection that will recur — A64-021.5 §11.

    A malformed address, a sender the provider will not accept, a body it
    cannot parse. Retrying is asking the same question and being told no
    again.

    **Here rather than in `notifications`**, because it is the transport's
    vocabulary: an adapter raises it and a caller reads it, and putting it in
    a bounded context would mean `platform` importing one to describe its own
    contract — which `.importlinter` forbids, correctly.

    There is deliberately no `TransientEmailFailure` beside it. Everything
    that is *not* this is retryable, including exceptions no adapter
    classified, and a second type would invite an adapter to raise neither
    and leave a caller with a third case to guess at.
    """


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

    async def send(self, message: EmailMessage) -> str | None:
        """Delivers and returns the provider's reference, or raises.

        Raises rather than returning a failure flag, because a caller that
        can meaningfully continue after a send failure is rare and should
        say so explicitly with a `try`. See `EmailVerificationService` for
        the one place that does, and why.

        **The exception type is the classification.** An implementation
        raises `PermanentEmailFailure` for a rejection that will recur — a
        malformed address, a sender the provider will not accept — and lets
        everything else propagate. That split belongs here rather than in a
        service because only the adapter can read a vendor's status code,
        and a service branching on one would be a service that knows a
        vendor.

        Returns `None` where a transport has no reference to give.
        """
        ...
