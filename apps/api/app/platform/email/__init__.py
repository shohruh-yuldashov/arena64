"""Outbound email transport — the port, the message, and the console.

`platform`, not a module, because two bounded contexts send mail: `auth`
sends a verification link and `notifications` sends a tournament
confirmation. See `message.py` on why one transport rather than two.

Nothing here knows what a user, a notification or a session is — the
`.importlinter` contract that forbids `platform` from importing a bounded
context is what keeps that true rather than a convention.
"""

from app.platform.email.console import ConsoleEmailProvider
from app.platform.email.message import EmailMessage, EmailProvider
from app.platform.email.provider import build_email_provider

__all__ = [
    "ConsoleEmailProvider",
    "EmailMessage",
    "EmailProvider",
    "build_email_provider",
]
