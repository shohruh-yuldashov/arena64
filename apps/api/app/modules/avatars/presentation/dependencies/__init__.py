"""The FastAPI `Depends` bridge for `avatars` — dependency-injection.md
DI-01: `Depends` is used only at the routing layer, to hand a route an
already-resolved service. It is not the container.

The graph assembled per request:

    AsyncSession            one per request (`app.api.deps`)
      -> SqlAlchemyUserRepository
      -> SessionUnitOfWork  the transaction `users` will commit
      -> UserService
      -> AvatarReferenceService  adapts it to the published `AvatarStore`
    StorageProvider         process singleton (`app.state`)
    PillowImageProcessor    stateless
    Clock                   injected, never read directly (AD-07)
      -> AvatarService

**`StorageProvider` arrives as the port**, resolved by `app.api.deps`, so
nothing in this file names `LocalStorageProvider` — and neither does
anything under `application/`. That is the structural half of A64-012.2's
"business logic must NEVER depend on local storage implementation": the
only module that knows which provider is running is `app_factory`, which
builds it.

Adding S3 is therefore a branch in `app_factory` and nothing here.
"""

from typing import Annotated

from fastapi import Depends

from app.api.deps import ClockDep, DbSessionDep, StorageProviderDep
from app.database.unit_of_work import SessionUnitOfWork
from app.modules.avatars.application.ports import ImageProcessor
from app.modules.avatars.application.services import AvatarService
from app.modules.avatars.infrastructure import PillowImageProcessor
from app.modules.avatars.public import AvatarLinkBuilder
from app.modules.users.application.services import UserService
from app.modules.users.application.services.avatar_reference_service import (
    AvatarReferenceService,
)
from app.modules.users.infrastructure.repositories import SqlAlchemyUserRepository
from app.modules.users.public import AvatarStore


def get_avatar_store(session: DbSessionDep, clock: ClockDep) -> AvatarStore:
    """`users`' side of the avatar reference, behind its seventh published
    port.

    Assembled separately from the other six for the reason they are
    separate from each other: writing an avatar reference is a distinct
    capability from creating an account, reading a password hash, reading a
    profile, confirming an address, replacing a credential or reading a
    public view. A single factory returning something that satisfied all
    seven would undo the split the ports exist to make.
    """
    users = UserService(
        users=SqlAlchemyUserRepository(session),
        unit_of_work=SessionUnitOfWork(session),
        clock=clock,
    )
    return AvatarReferenceService(users)


AvatarStoreDep = Annotated[AvatarStore, Depends(get_avatar_store)]


def get_image_processor() -> ImageProcessor:
    """The Pillow pipeline.

    Stateless, so a per-request instance costs one attribute assignment
    against the tens of milliseconds of decoding it performs — the same
    measurement A64-011.1 made for the Argon2 hasher, with the same
    conclusion.
    """
    return PillowImageProcessor()


ImageProcessorDep = Annotated[ImageProcessor, Depends(get_image_processor)]


def get_avatar_link_builder(storage: StorageProviderDep) -> AvatarLinkBuilder:
    """Renders stored references into URLs.

    Exported through `avatars.public` and injected by `profiles` as well as
    by this module's own routes, so that the key layout and the provider
    are known in exactly one place.
    """
    return AvatarLinkBuilder(storage)


AvatarLinkBuilderDep = Annotated[AvatarLinkBuilder, Depends(get_avatar_link_builder)]


def get_avatar_service(
    avatars: AvatarStoreDep,
    storage: StorageProviderDep,
    processor: ImageProcessorDep,
    clock: ClockDep,
) -> AvatarService:
    """The upload, read and delete use cases.

    Every collaborator arrives already resolved rather than being built
    inline, so this factory cannot accidentally construct a second
    `UserService` on a different session — the mistake `auth`'s
    `get_password_reset_service` documents at length.
    """
    return AvatarService(
        avatars=avatars,
        storage=storage,
        processor=processor,
        clock=clock,
    )


AvatarServiceDep = Annotated[AvatarService, Depends(get_avatar_service)]


__all__ = [
    "AvatarLinkBuilderDep",
    "AvatarServiceDep",
    "AvatarStoreDep",
    "ImageProcessorDep",
    "get_avatar_link_builder",
    "get_avatar_service",
    "get_avatar_store",
    "get_image_processor",
]
