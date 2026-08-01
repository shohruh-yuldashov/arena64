"""`PublicProfileSearchService` — the implementation behind
`users.public.ports.PublicProfileSearcher`.

The same shape as `PublicProfileService` beside it, and deliberately so:
both turn a lookup into `PublicUserProfile` values, both apply
`to_public_profile`, and neither has a rule of its own. What differs is only
which lookup — one username, or a ranked page.

That symmetry is the point rather than a coincidence. A64-013.1 requires
search results to be the same public representation as profile pages, and
the strongest form of that is the two paths sharing a *mapper*: `country` is
redacted by `to_public_profile` before either port returns, so a consumer
cannot render a hidden country from a search result any more than it can
from a profile page.

## Why this is a service and not the repository returning DTOs

repositories.md keeps mapping between rows and *domain entities* in the
repository, and mapping between entities and *published DTOs* in the
application layer. The repository therefore returns `User` — which carries a
password hash — and this class is the boundary where that stops being true.

The consequence worth stating: nothing outside `users` can obtain a `User`
from a search, however many results it asks for.
"""

import logging

from app.modules.users.application.mappers import to_public_profile
from app.modules.users.application.services.user_service import UserService
from app.modules.users.public.search import UserSearchPage, UserSearchQuery

logger = logging.getLogger(__name__)


class PublicProfileSearchService:
    def __init__(self, users: UserService) -> None:
        self._users = users

    async def search_public_profiles(self, query: UserSearchQuery) -> UserSearchPage:
        """One page of matching players, already redacted.

        No `try`/`except` around the lookup, unlike
        `PublicProfileService.find_public_profile`. That one swallows
        `UserNotFound` and `InvalidUsername` because a *single* lookup has
        to answer "no such player" without an exception a caller could time.
        A search has no equivalent: a term nobody matches is an empty page,
        which the repository returns without raising, and the two rejections
        that *can* occur here — a malformed term, a foreign cursor — are
        genuine client errors that must reach the caller as a `422`.

        Logs nothing. The operationally interesting line —
        length, count, duration — is emitted once by
        `ProfileSearchService`, which is the layer that knows all three;
        emitting a second here would double every search in the log for no
        additional fact.
        """
        users, next_cursor = await self._users.search_users(query)

        return UserSearchPage(
            # Mapped one by one through the same function the profile page
            # uses. `tuple` because `UserSearchPage` is frozen and the rank
            # order must survive: a caller handed a list could sort it and
            # discard the only thing the query computed.
            identities=tuple(to_public_profile(user) for user in users),
            next_cursor=next_cursor,
        )
