"""The only package other modules may import from `avatars` — BE-03.

One thing is published, and it exists for exactly one consumer:
`profiles`, which renders an avatar on every public profile and must not
learn either the key layout or the storage provider to do it.

  `AvatarLinks`        a pair of ready-to-use URLs, or nothing
  `AvatarLinkBuilder`  turns an `AvatarReference` into that pair

Deliberately **not** published: `AvatarService`, `ImageProcessor`,
`AvatarKey`, and every domain constant. A module that could reach
`AvatarService` could replace any player's avatar; a module that could
build an `AvatarKey` would be a second place the storage layout is written
down, and the two would drift the first time it changed.

The asymmetry with `users.public` is worth noting: that module publishes
*ports* for consumers to call, because consumers write to it. Nothing
writes to `avatars` except its own endpoints, so what is published here is
a **renderer** rather than a port — the read direction only.
"""

from app.modules.avatars.public.links import AvatarLinkBuilder, AvatarLinks

__all__ = ["AvatarLinkBuilder", "AvatarLinks"]
