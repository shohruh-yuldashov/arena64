"""The **only** package other modules may import from `profiles` — BE-03.

Empty until A64-013.7, and that emptiness was correct: `profiles` is a
*composition* module. It consumes `users`, `statistics`, `avatars` and
`friends` and, until this task, nothing consumed it — every one of its
readers arrived over HTTP.

A64-013.7 added the first non-HTTP reader. A background worker must render a
notification payload "through `PublicProfileComposer`", which is exactly
what this module exists to do and exactly what nothing outside it should be
able to reassemble.

What is published, and why only this much:

  `ProfileRenderer`   renders many players' public views under one asserted
                      relationship, in a fixed number of reads. The privacy
                      gate is inside it and cannot be skipped
  `PublicProfile`     what it returns — published because BR-2 requires a
                      published port's result type to be published too

Deliberately **not** published: `PublicProfileComposer` itself (a consumer
holding it could compose with a relationship it invented, and would have to
know how to build its four providers), `ProfileService`, `ProfileDirectory`,
`ProfileSearchService`, and every schema under `presentation/`.

## Why the port takes a relationship rather than a viewer id

A viewer id would make this the fourth place on the platform that resolves
"what is this pair to each other", and the resolution is `friends`'. The
relationship is instead an **assertion by the caller**, valid only where the
audience's membership defines it — see `BatchProfileRenderer` and
`SocialNotificationDispatcher`, which is where each assertion is argued.
"""

from app.modules.profiles.domain.profile import PublicProfile
from app.modules.profiles.public.ports import ProfileRenderer

__all__ = ["ProfileRenderer", "PublicProfile"]
