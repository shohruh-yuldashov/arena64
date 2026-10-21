"""An in-memory `ExternalIdentityRepository` — A64-031.E1.

Reproduces the two unique constraints as the same typed exception the
SQLAlchemy adapter raises, so a service test takes the branch production
takes. It does not reproduce the transaction: that one account and its
link commit or roll back together is a property of PostgreSQL, asserted in
`tests/contract/test_external_identity_repository.py`.
"""

from datetime import datetime
from uuid import UUID

from app.modules.users.domain.exceptions import ExternalIdentityAlreadyLinked
from app.modules.users.domain.external_identity import ExternalIdentity


class FakeExternalIdentityRepository:
    def __init__(self) -> None:
        self.links: dict[ExternalIdentity, UUID] = {}

    async def user_id_for(self, identity: ExternalIdentity) -> UUID | None:
        return self.links.get(identity)

    async def add(self, user_id: UUID, identity: ExternalIdentity, *, created_at: datetime) -> None:
        provider_taken = any(
            owner == user_id and linked.provider is identity.provider
            for linked, owner in self.links.items()
        )
        if identity in self.links or provider_taken:
            raise ExternalIdentityAlreadyLinked("That account is already connected.")
        self.links[identity] = user_id
