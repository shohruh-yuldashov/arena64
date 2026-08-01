"""The `avatars` module — image bytes in, object keys out.

## What it owns

The whole journey of an uploaded image: refusing what must not be accepted,
stripping what must not be kept, resizing, re-encoding, storing two
renditions, and pointing the account at them. Plus the three endpoints that
drive it.

## What it deliberately does not own

**The column.** `users.user.avatar_object_key` belongs to `users`
(domain-model.md §7: `UserProfile` owns "avatar reference"), and this
module reaches it through the published `AvatarStore` port. The split is
the same one `auth` and `users` draw over passwords — one module owns the
credential material and its rules, the other owns the column — and it is
why `users` has no idea what an image is.

**The storage.** `StorageProvider` is a platform port
(`app.core.storage`), implemented in `app/storage/`. This module names
keys and hands over bytes; where those bytes physically land is a
deployment's business. That is the requirement A64-012.2 states as
"business logic must NEVER depend on local storage implementation", and it
holds structurally: nothing under `application/` or `domain/` imports
`app.storage`.

**Rendering a URL.** Composing a key into something a browser can fetch is
`StorageProvider.get_public_url`'s. This module publishes an
`AvatarLinkBuilder` (`public/`) so `profiles` can do it during response
mapping without learning either the key layout or the provider.

## Layout

Mirrors every other module (`domain` / `application` / `infrastructure` /
`presentation` / `public`), per services.md §2.1: uniformity is worth more
than local optimisation, because a contributor who has read one module can
navigate the next.
"""
