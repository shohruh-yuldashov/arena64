"""Object keys — where an avatar lives in the store, and how its thumbnail
is found.

Framework-free (architecture.md §8). A key is a string; composing one needs
no storage, and keeping the layout here means one module decides it and
every other reads it.

## Why the thumbnail key is derived rather than stored

A64-012.2 says to store **only** `avatar_object_key`. So the thumbnail must
be findable from the original, which makes the naming convention
load-bearing: `delete` has to remove a file whose name it was never told.

`AvatarKey` is that convention expressed once. Both renditions come from
one `AvatarKey` instance, so the writer and the deleter cannot disagree
about the thumbnail's name — which is the failure that leaves an orphaned
128px file behind on every delete, invisibly, until a storage bill grows.

    avatars/{user_id}/{token}.webp        the original, at most 512px
    avatars/{user_id}/{token}_thumb.webp  the thumbnail, at most 128px

## Why the filename is random rather than derived from anything

`secrets.token_urlsafe`, not the user id, not a hash of the image, not the
uploaded filename.

**Never the uploaded filename** — A64-012.2 states it and the reasons
compound: it is attacker-controlled, it can contain path separators, it can
be `../../etc/passwd`, it can be 4000 characters, it can differ only by
case on a case-insensitive filesystem, and it can carry the uploader's real
name from their desktop into a public URL.

**Not a content hash**, which would be the tidy choice: identical images
would then share a key, so deleting one player's avatar would delete
another's, and the existence of a key would leak that two accounts uploaded
the same picture.

**Random per upload**, which additionally means a replaced avatar always
gets a new URL. That is what makes the object safely cacheable forever, and
it is why `avatar_version` is a second lock rather than the only one.

## Why the user id is in the path

It buys operational legibility — "show me this player's objects" is a
prefix listing — and a natural blast radius for a future bulk erasure
(DM-13 requires the avatar to be deleted on anonymisation).

It does mean the key discloses a player id to anyone holding the URL. That
is acceptable: the id is already public (DM-06 makes it the cross-context
reference, and `GET /profiles/{username}` returns it). Nothing about
knowing it grants access to anything.
"""

import secrets
from dataclasses import dataclass
from typing import Final
from uuid import UUID

from app.modules.avatars.domain.images import STORED_EXTENSION

#: Every avatar object lives under this prefix, so a store can carry other
#: object kinds (exports, per architecture.md §134) without collision.
KEY_PREFIX: Final[str] = "avatars"

#: Distinguishes the thumbnail from the original within one upload.
THUMBNAIL_SUFFIX: Final[str] = "_thumb"

#: Bytes of randomness in a filename. 16 bytes is 128 bits — far beyond
#: any collision concern, and beyond guessing even if a key were somehow
#: worth guessing (it is not: the object is public).
_TOKEN_BYTES: Final[int] = 16


@dataclass(frozen=True, slots=True)
class AvatarKey:
    """One upload's pair of object keys.

    Frozen, and constructed either fresh (`generate`) or by parsing a
    stored original (`from_object_key`). Those two constructors are the
    only ways to obtain one, which is what stops a caller from hand-building
    a key with a thumbnail name the deleter will not recognise.
    """

    user_id: UUID
    token: str

    @classmethod
    def generate(cls, user_id: UUID) -> "AvatarKey":
        """A new, unguessable key pair for this player."""
        return cls(user_id=user_id, token=secrets.token_urlsafe(_TOKEN_BYTES))

    @classmethod
    def from_object_key(cls, object_key: str) -> "AvatarKey | None":
        """Recovers the pair from a stored original key, or `None` if the
        string is not one this platform wrote.

        Needed by `delete`, which holds only what the database stored and
        must find the thumbnail beside it.

        Returns `None` rather than raising for an unrecognised shape,
        because the caller's response is the same either way: delete what
        it can identify and log the rest. A key written by an older layout,
        or corrupted, should not make an account's avatar undeletable — see
        `AvatarService.delete`.

        Deliberately strict about the shape. It does not, for instance,
        accept a key whose user segment is not a UUID: a permissive parser
        here is one that could be induced to return a key pointing at
        another player's object.
        """
        parts = object_key.split("/")
        if len(parts) != 3 or parts[0] != KEY_PREFIX:
            return None

        _, raw_user_id, filename = parts
        expected_suffix = f".{STORED_EXTENSION}"
        if not filename.endswith(expected_suffix):
            return None

        token = filename[: -len(expected_suffix)]
        if not token or THUMBNAIL_SUFFIX in token:
            return None

        try:
            user_id = UUID(raw_user_id)
        except ValueError:
            return None

        return cls(user_id=user_id, token=token)

    @property
    def original(self) -> str:
        """The key of the full-size rendition — the one stored in
        `users.user.avatar_object_key`."""
        return f"{KEY_PREFIX}/{self.user_id}/{self.token}.{STORED_EXTENSION}"

    @property
    def thumbnail(self) -> str:
        """The key of the 128px rendition, derived from the original.

        Derived rather than stored — see this module's docstring. This
        property and `original` are the only two places the layout is
        written down, which is what makes the writer and the deleter agree
        by construction.
        """
        return f"{KEY_PREFIX}/{self.user_id}/{self.token}{THUMBNAIL_SUFFIX}.{STORED_EXTENSION}"

    @property
    def keys(self) -> tuple[str, str]:
        """Both renditions, original first. The order matters to
        `AvatarService.delete`, which removes the thumbnail before the
        original so a crash cannot leave the *referenced* object missing
        while its derivative survives."""
        return (self.original, self.thumbnail)
