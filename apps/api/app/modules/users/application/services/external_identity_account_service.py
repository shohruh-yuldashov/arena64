"""`ExternalIdentityAccountService` — the implementation behind
`users.public.ports.ExternalIdentityAccounts`, A64-031.E1.

The same shape as `UserCredentialService`: a thin translation between
`UserService`'s domain types and the published ones, with no rule of its
own. Every rule — uniqueness, the transaction, verification on creation —
is `UserService`'s.
"""

from uuid import UUID

from app.modules.users.application.commands import CreateUser
from app.modules.users.application.mappers import to_user_credentials, to_user_read
from app.modules.users.application.services.user_service import UserService
from app.modules.users.domain.external_identity import ExternalIdentity, IdentityProvider
from app.modules.users.public.credentials import UserCredentials
from app.modules.users.public.dtos import UserRead
from app.modules.users.public.ports import NewUserAccount


class ExternalIdentityAccountService:
    def __init__(self, users: UserService) -> None:
        self._users = users

    async def find_credentials_by_external_identity(
        self, provider: IdentityProvider, subject: str
    ) -> UserCredentials | None:
        identity = ExternalIdentity(provider, subject)
        return to_user_credentials(await self._users.lookup_by_external_identity(identity))

    async def create_with_external_identity(
        self, account: NewUserAccount, *, provider: IdentityProvider, subject: str
    ) -> UserRead:
        created = await self._users.create_user_with_external_identity(
            CreateUser(
                username=account.username,
                email=account.email,
                password_hash=account.password_hash,
                preferred_language=account.preferred_language,
                timezone=account.timezone,
                display_name=account.display_name,
            ),
            ExternalIdentity(provider, subject),
        )
        return to_user_read(created)

    async def link_external_identity(
        self, user_id: UUID, *, provider: IdentityProvider, subject: str
    ) -> None:
        await self._users.link_external_identity(user_id, ExternalIdentity(provider, subject))
