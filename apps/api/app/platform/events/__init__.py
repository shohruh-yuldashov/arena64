"""The domain-event contract — AD-16's vocabulary.

Deliberately holds no *catalogue*. Every concrete event is defined by the
context that owns the fact (`friends.domain.events`, `users.domain.events`)
and published through that context's `public/` surface, because an event is
part of a bounded context's contract in exactly the way a DTO is (BE-03).

A central catalogue would make every module import a file that every other
module writes to — the shared-mutable-header shape that turns a boundary
into a suggestion.
"""

from app.platform.events.domain_event import DomainEvent

__all__ = ["DomainEvent"]
