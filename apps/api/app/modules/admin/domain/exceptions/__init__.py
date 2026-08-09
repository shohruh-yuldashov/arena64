"""What `admin` refuses, as types — CLAUDE.md §9.5.

A caller branches on the type rather than on a message, and each maps to
exactly one outcome where a route or an operator command surfaces it. They
subclass the platform's existing taxonomy so the shared exception handlers
render them without this module owning a response shape.
"""

from app.core.exceptions import ConflictError, ValidationError


class SelfGrant(ValidationError):
    """An administrator tried to grant a role to themselves.

    The escalation `AdminRoleService` exists to prevent: if holding the
    ability to call `grant` were enough to acquire the role, the guard
    would be decoration.
    """


class AlreadyGranted(ConflictError):
    """A live grant of this role already exists for this account.

    Also raised by `bootstrap` when the role already has any holder — at
    that point an administrator exists who can grant the next one, and the
    unattributed path must close behind itself.
    """


class NotGranted(ConflictError):
    """There is no live grant to revoke."""


class LastAdministrator(ConflictError):
    """Revoking would leave the platform with no administrator.

    A state no route could recover from: granting requires an
    administrator, and `bootstrap` refuses while a holder exists. So the
    refusal is not politeness — it is the only thing between a mistyped
    command and a deployment that has to be repaired by hand.
    """


class SelfSanction(ValidationError):
    """An administrator tried to restrict their own account — A64-024.6.

    Refused for the same reason `SelfGrant` is, inverted: an administrator
    who can withhold their own access can lock the console's operator out
    of the surface they are operating, and §13.2 already forbids acting on
    a case involving oneself.
    """


class ProtectedAdministrator(ConflictError):
    """Restricting this account would leave the platform unadministrable.

    The moderation counterpart of `LastAdministrator`. A suspension is not
    a role revocation, but a suspended administrator cannot sign in — so
    suspending the last one removes every route back into the console just
    as surely, and with no `bootstrap` to recover through.
    """


class AlreadySanctioned(ConflictError):
    """An effective sanction of this kind already exists for this account.

    A conflict rather than a silent success: the second call would
    otherwise write a second case and a second audit row for a state
    transition that did not happen, and a trail that records transitions
    which never occurred is worse than one that records too few.
    """


class NotSanctioned(ConflictError):
    """There is no effective sanction of this kind to lift."""


__all__ = [
    "AlreadyGranted",
    "AlreadySanctioned",
    "LastAdministrator",
    "NotGranted",
    "NotSanctioned",
    "ProtectedAdministrator",
    "SelfGrant",
    "SelfSanction",
]
