"""The only package other modules may import from `auth` — BE-03.

A64-011.2 published nothing, on the grounds that no module had a reason
to ask `auth` a question yet. A64-011.3 gives every module the same one:
*who is making this request*.

What is published, and why only this much:

  `AuthenticatedUser`   the proven identity a route receives — an id and
                        the token facts behind it, nothing more

Deliberately **not** published: `TokenProvider`, `TokenValidator`,
`AccessTokenService`, `TokenClaims`, or anything that could mint or
inspect a token. A module that could issue tokens could issue one for any
account; a module that could decode them would be a second place where
audience and expiry checking has to be got right. Every other module
consumes the *result* of authentication through the dependencies in
`auth.presentation.dependencies`, and never the machinery.
"""

from app.modules.auth.public.principal import AuthenticatedUser

__all__ = ["AuthenticatedUser"]
