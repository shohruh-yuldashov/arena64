"""Choosing the transport — A64-021.5 §12, §36.

One function, and it is the **only** place on this platform that decides
which `EmailProvider` a process holds. `auth`'s verification mail and
`notifications`' tournament mail both come through here, which is what makes
"one sender identity, one credential, one retry story" a property of the
code rather than a note in a document.

## No vendor has been selected, and this is where that is visible

A64-021.5 built the whole notification email channel — the delivery table,
the retry policy, the templates, the worker — and stopped short of an
adapter, because choosing a vendor is a product and billing decision and
picking one silently is the thing §36 forbids. What is needed to finish it
is small and specific, and is recorded in `specs/notifications.md` §16.

Until then this returns `ConsoleEmailProvider`, which **refuses to construct
in a production-like environment**. So the failure mode of deploying without
a decision is a process that will not start — a visible, rolled-back deploy
— rather than a platform that silently sends nobody anything.

The shape a vendor adapter takes is already fixed by `EmailProvider`: one
class in this package, one branch here, and no service changes. It must
raise `PermanentEmailFailure` for a rejection that will recur and let
everything else propagate, which is how `EmailDeliveryService` classifies a
retry (§11) — the classification belongs to the adapter because only it can
read a vendor's status code.
"""

from app.config.environment import Environment
from app.platform.email.console import ConsoleEmailProvider
from app.platform.email.message import EmailProvider


def build_email_provider(environment: Environment) -> EmailProvider:
    """The transport this process sends through.

    Takes an `Environment` rather than the whole `Settings`, so the choice
    cannot come to depend on something unrelated to transport — and so a
    caller can see from the signature that nothing here reads a database, a
    feature flag or a user.
    """
    return ConsoleEmailProvider(environment)


__all__ = ["build_email_provider"]
