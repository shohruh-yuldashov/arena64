"""`PublicProfileService` — the implementation behind
`users.public.ports.PublicProfileReader`.

The same shape as the other five published-port adapters, and for the same
reason: a thin translation between `UserService`'s domain types and the
published DTO, with no rule of its own. See `user_account_service.py` on
why that translation is not skippable.

What is specific to this one is the *absence* it maps to. `UserService`
has two username lookups — `find_by_username`, which raises `UserNotFound`,
and nothing else. This port needs the non-raising form, for the reason its
docstring gives: on a public endpoint an exception is a branch, and a
branch is a thing that can be timed. So the exception is caught here and
turned into `None`, once, rather than at every call site.

Catching rather than adding a third lookup to `UserService` is deliberate.
`lookup_by_email` exists as the non-raising twin of `find_by_email`
because the *sign-in* path needs the two code paths to cost the same, and
that is a claim about `users`' internals. Here the requirement is only
that the caller sees `None`, which is an adapter's job.

## Deactivated accounts are invisible, and the rule lives here

An account with `is_active=False` yields `None`, exactly as an unknown
username does.

The rule is enforced on this side of the port rather than in `profiles`,
for a reason that is easy to get backwards. `profiles` decides which
*fields* a stranger sees; `users` owns `is_active` and therefore owns
whether the account is publicly readable at all. Pushing the decision
outward would mean publishing `is_active` on `PublicUserProfile` so the
consumer could act on it — which is the one thing that DTO must not do,
since "which accounts are deactivated" is itself a disclosure. Deciding it
here means a deactivated account never crosses the boundary in any form.

Two reasons it is invisible rather than rendered with a flag. A
deactivated account is one whose owner has withdrawn or one an
administrator has removed from view, and continuing to serve their display
name, country and biography to anonymous callers honours neither. And
publishing which accounts are deactivated tells anyone holding a list of
usernames who has left — precisely the signal an impersonator wants when
choosing whom to imitate (UP-3 makes the same argument about released
handles).

`is_verified` is deliberately *not* part of this rule. An unverified
account is a real, live account whose owner has not clicked a link; hiding
it would make the profile appear and disappear on an action the visitor
cannot see.
"""

from collections.abc import Mapping, Sequence
from uuid import UUID

from app.modules.users.application.mappers import to_public_profile
from app.modules.users.application.services.user_service import UserService
from app.modules.users.domain.exceptions import InvalidUsername, UserNotFound
from app.modules.users.public.dtos import PublicUserProfile


class PublicProfileService:
    def __init__(self, users: UserService) -> None:
        self._users = users

    async def find_public_profile(self, username: str) -> PublicUserProfile | None:
        try:
            user = await self._users.find_by_username(username)
        except UserNotFound:
            return None
        except InvalidUsername:
            # A name that cannot be valid cannot belong to anyone, so this
            # is "not found" rather than "bad request" — and answering it
            # the same way is what stops the endpoint from telling a
            # scraper which of its guesses were even *shaped* like real
            # usernames. `AuthenticationService._find_credentials` makes
            # the identical call for the identical reason.
            #
            # The route still rejects a malformed name earlier with a
            # path-parameter 422, which is the right feedback for a typo.
            # This is the guard for every other caller.
            return None

        if not user.is_active:
            # Indistinguishable from "no such username", by construction:
            # the same `None`, from the same method, with nothing for a
            # caller to branch on. See this module's docstring.
            return None

        return to_public_profile(user)

    async def find_public_profiles(
        self, player_ids: Sequence[UUID]
    ) -> Mapping[UUID, PublicUserProfile]:
        """The batch form — A64-013.2.

        Keyed by id so a caller holding an ordering of its own (a
        friend-request list is ordered by when the request arrived) can
        index rather than search.

        **Deactivated accounts are absent from the mapping**, because
        `find_active_by_ids` never returns them. That is the same rule the
        single lookup applies, expressed as omission rather than as `None`
        — and it means a consumer cannot render a withdrawn account even by
        iterating the ids it asked about.
        """
        users = await self._users.find_active_by_ids(player_ids)
        return {user.id: to_public_profile(user) for user in users}
