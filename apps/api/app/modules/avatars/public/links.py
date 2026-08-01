"""Turning a stored reference into URLs a browser can fetch.

The seam A64-012.2 asks for when it says "avatar URL must be generated
during response mapping" and "`ProfileResponse` should not know storage
implementation details". Two collaborators meet here and neither is
exposed to the consumer: the **key layout** (this module's) and the
**provider** (`StorageProvider`'s).

`profiles` injects an `AvatarLinkBuilder`, calls one method, and renders
two strings. It never sees a key, a bucket, a base URL or a thumbnail
naming convention.

## Why the version is a query parameter rather than part of the key

The object key is already random per upload, so a *replaced* avatar is
already a new URL and caching the old one forever is already safe. The
`?v=` is the second lock, and it earns its place on the case the random
key does not cover: an intermediary that keyed on something coarser than
the full URL, or a client that stored "this player's avatar URL" and needs
a signal that it changed.

A query parameter rather than a path segment because it must **not** change
the object's address — the store holds one object, and `v=3` and `v=4`
have to resolve to the same bytes. Putting the version in the path would
mean re-uploading the same image under a new key on every bump, which is
the opposite of what a cache-buster is for.
"""

from dataclasses import dataclass

from app.core.storage import StorageProvider
from app.modules.avatars.domain.keys import AvatarKey
from app.modules.users.public import AvatarReference

#: The cache-busting query parameter. Short because it lands in every
#: avatar URL on every page.
VERSION_PARAMETER = "v"


@dataclass(frozen=True, slots=True)
class AvatarLinks:
    """Both renditions' URLs, ready to render.

    A frozen dataclass rather than a Pydantic model: it is an intermediate
    value, and each consuming module declares its own wire schema. Two
    modules already render these — `avatars` on its own endpoints and
    `profiles` on the public profile — and a shared response model would
    couple their API versioning together.
    """

    avatar_url: str
    thumbnail_url: str


class AvatarLinkBuilder:
    """Composes avatar URLs. Holds a provider and nothing else.

    Stateless and safe to share; constructed per request because the
    provider is (see `app/api/deps.py`).
    """

    def __init__(self, storage: StorageProvider) -> None:
        self._storage = storage

    def links_for(self, reference: AvatarReference) -> AvatarLinks | None:
        """URLs for a stored avatar, or `None` when the player has none.

        `None` rather than a pair of empty strings or a placeholder URL.
        "This player has no avatar" is a rendering decision — a generated
        identicon, a coloured initial, a default image — and it belongs to
        the client, which knows the size and the surrounding design. A
        backend that substituted its own would make that decision for every
        client at once and would make "has an avatar" untestable from the
        response.

        Returns `None` too when the stored key is not one this platform
        wrote, so a corrupted or legacy row renders as "no avatar" rather
        than as a broken image. The alternative — deriving a thumbnail name
        from an unparseable key — produces a URL that 404s, which is worse
        because it looks like it should work.
        """
        if reference.object_key is None:
            return None

        key = AvatarKey.from_object_key(reference.object_key)
        if key is None:
            return None

        return AvatarLinks(
            avatar_url=self._versioned(key.original, reference.version),
            thumbnail_url=self._versioned(key.thumbnail, reference.version),
        )

    def _versioned(self, object_key: str, version: int) -> str:
        """`get_public_url` plus the cache-buster.

        The provider composes the address; this appends the version. Which
        is the whole division of labour — this class knows there *is* a
        version, and the provider knows what a URL looks like.
        """
        return f"{self._storage.get_public_url(object_key)}?{VERSION_PARAMETER}={version}"
