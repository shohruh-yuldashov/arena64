"""`SqlAlchemyExternalIdentityRepository` — `users.external_identity`,
A64-031.E1.

Two operations and no more: resolve an identity to its account, and record
a new one. There is no update (a link is never edited) and no delete (an
identity leaves with its account, by `CASCADE`).
"""

import logging
from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.users.domain.exceptions import ExternalIdentityAlreadyLinked
from app.modules.users.domain.external_identity import ExternalIdentity
from app.modules.users.infrastructure.models import UserExternalIdentityModel
from app.modules.users.infrastructure.repositories.user_repository import violated_constraint

logger = logging.getLogger(__name__)

#: Either constraint means the same thing to a caller: this link cannot be
#: made because one side of it is already taken.
_LINK_CONSTRAINTS = frozenset(
    {"uq_external_identity__provider_subject", "uq_external_identity__user_provider"}
)


class SqlAlchemyExternalIdentityRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def user_id_for(self, identity: ExternalIdentity) -> UUID | None:
        """The account this identity signs in to. One probe on
        `uq_external_identity__provider_subject`."""
        statement = select(UserExternalIdentityModel.user_id).where(
            UserExternalIdentityModel.provider == identity.provider.value,
            UserExternalIdentityModel.subject == identity.subject,
        )
        return (await self._session.scalars(statement)).one_or_none()

    async def add(self, user_id: UUID, identity: ExternalIdentity, *, created_at: datetime) -> None:
        """Records the link. Flushes so a violated constraint raises here,
        translated, rather than at the caller's commit — and never commits,
        because the unit of work owns the transaction."""
        self._session.add(
            UserExternalIdentityModel(
                user_id=user_id,
                provider=identity.provider.value,
                subject=identity.subject,
                created_at=created_at,
            )
        )
        try:
            await self._session.flush()
        except IntegrityError as error:
            constraint = violated_constraint(error)
            if constraint in _LINK_CONSTRAINTS:
                raise ExternalIdentityAlreadyLinked(
                    "That account is already connected to another sign-in."
                ) from error
            logger.error(
                "unmapped_integrity_error", extra={"constraint": constraint}, exc_info=error
            )
            raise
