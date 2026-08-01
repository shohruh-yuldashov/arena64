"""`GET /users/search` — find players by handle or display name.

One endpoint, and **no business logic in it**. The handler translates three
query parameters into a service call and the result into a wire schema.
Term validation is the domain's, ranking is the query's, composition and
privacy are `PublicProfileComposer`'s, and the exclusion set is
`ProfileSearchService`'s.

## Why this router lives in `profiles` and serves a `/users` path

The path is A64-013.1's, and the module is where the work is: a search
result *is* a public profile, composed from four sources and gated by four
privacy flags, and `profiles` is the module that owns that composition. A
copy of it under `users` would be a second public representation of a
player, which is the one thing A64-013.1 forbids.

Serving a path that does not match the module name is established practice
here rather than a new liberty — `avatars` serves `/profile/avatar` for the
same kind of reason.

**Registration order matters and is not incidental.** This router must be
included before `users_router`, because `GET /users/{user_id}` declares a
`UUID` path parameter and would otherwise match `/users/search` first and
reject `search` as a malformed identifier. `app/api/v1/router.py` says so at
the include, and `tests/contract/test_user_search_api.py` asserts the
resolution rather than trusting the comment.

## Authenticated, and that is a security control rather than a default

Every other public read of a profile on this platform is anonymous, and
this one is not. Two reasons that reinforce each other:

  - A64-013.1 requires **per-user** rate limiting, and a per-user budget
    needs a user. The alternative dimension — per IP — is defeated by a
    botnet and punishes a shared campus or carrier connection.
  - Search is the platform's enumeration surface. Requiring a token means
    building a directory costs an attacker a registration per budget, and
    registration is itself rate limited to three per hour per address.

The consequence is worth stating plainly: a signed-out visitor can read any
profile they know the handle of, and cannot discover handles they do not.
That asymmetry is the design.

## Errors need no handling here

Every failure is a typed exception on the platform hierarchy, and
`app/api/exception_handlers.py` maps them by MRO walk. There is not one
`try`/`except` in this file:

    InvalidSearchTerm    -> 422  validation_error
    InvalidSearchCursor  -> 422  validation_error
    MissingToken         -> 401  authentication_required
    TooManyRequests      -> 429  rate_limited
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.api.openapi import Responses, error_response
from app.api.responses import build_response
from app.core.pagination import CursorPage, CursorPageInfo
from app.core.responses import ApiResponse
from app.modules.auth.presentation.dependencies import CurrentUser
from app.modules.avatars.presentation.dependencies import AvatarLinkBuilderDep
from app.modules.profiles.presentation.dependencies import ProfileSearchServiceDep
from app.modules.profiles.presentation.rate_limits import enforce_search_limit
from app.modules.profiles.presentation.schemas import ProfileResponse
from app.modules.users.domain.search import (
    DEFAULT_SEARCH_PAGE_SIZE,
    MAX_SEARCH_PAGE_SIZE,
    SEARCH_TERM_MAX_LENGTH,
    SEARCH_TERM_MIN_LENGTH,
)

user_search_router = APIRouter(prefix="/users", tags=["search"])

_UNAUTHORIZED: Responses = error_response(
    401, "No access token was presented, or it was invalid or expired."
)
_UNPROCESSABLE: Responses = error_response(
    422,
    (
        "The search term is empty, shorter than "
        f"{SEARCH_TERM_MIN_LENGTH} characters, longer than "
        f"{SEARCH_TERM_MAX_LENGTH}, carries a wildcard, or contains no letter or "
        "digit — or the cursor is malformed or belongs to a different term. "
        "`message` says which."
    ),
)
_TOO_MANY_REQUESTS: Responses = error_response(
    429,
    (
        "Too many searches from this account. Counted **per user**, not per network "
        "address, so a shared connection is never somebody else's problem. "
        "`Retry-After` says how long to wait."
    ),
)


@user_search_router.get(
    "/search",
    status_code=status.HTTP_200_OK,
    summary="Search for players",
    response_description="A ranked page of matching players.",
    responses={**_UNAUTHORIZED, **_UNPROCESSABLE, **_TOO_MANY_REQUESTS},
    dependencies=[Depends(enforce_search_limit)],
)
async def search_users(
    user: CurrentUser,
    service: ProfileSearchServiceDep,
    avatar_links: AvatarLinkBuilderDep,
    q: Annotated[
        str,
        Query(
            min_length=SEARCH_TERM_MIN_LENGTH,
            max_length=SEARCH_TERM_MAX_LENGTH,
            description=(
                f"What to search for — {SEARCH_TERM_MIN_LENGTH}-{SEARCH_TERM_MAX_LENGTH} "
                "characters, matched against usernames and display names. Surrounding "
                "whitespace is trimmed. Case-insensitive, and accent-insensitive for "
                "display names. Wildcards are not supported and are rejected."
            ),
            examples=["ali"],
        ),
    ],
    limit: Annotated[
        int,
        Query(
            ge=1,
            le=MAX_SEARCH_PAGE_SIZE,
            description="Results per page.",
        ),
    ] = DEFAULT_SEARCH_PAGE_SIZE,
    cursor: Annotated[
        str | None,
        Query(
            description=(
                "Opaque cursor from a previous page. Pass it back unchanged **with the "
                "same `q`** — a cursor is bound to the term it was issued for, and "
                "presenting it alongside a different one returns `422` rather than an "
                "arbitrary slice."
            ),
        ),
    ] = None,
) -> ApiResponse[CursorPage[ProfileResponse]]:
    """Finds players whose username or display name matches `q`.

    Returns the **same** profile representation as
    `GET /profiles/{username}`, field for field, including the privacy
    behaviour: a hidden country, record or presence is `null` here exactly as
    it is there, and a `null` never says which of its reasons applies.

    **Authenticated.** Unlike every other profile read on this platform, this
    one requires a token — see this module's docstring on why that is the
    enumeration control rather than an inconsistency.

    ## Ordering

    Ranked, in four buckets, then alphabetically inside each:

    | Rank | Match |
    | --- | --- |
    | 1 | the username is exactly `q` |
    | 2 | the username starts with `q` |
    | 3 | the display name starts with `q` |
    | 4 | `q` appears anywhere in either |

    Stable for a given term: the same query returns the same order, which is
    what makes the cursor correct rather than approximate.

    ## Pagination

    Keyset, never offset. Follow `page.next_cursor` until it is `null`;
    `page.has_more` says whether there is one. There is deliberately **no
    total**: counting a partial-match query costs as much as running it, and
    a count is not what somebody looking for a person needs.

    ## What is never returned

    Deactivated accounts, and yourself. Neither appears at any rank, under
    any term — the first because which handles belong to withdrawn accounts
    is a disclosure, the second because a search whose purpose is finding
    other people should not spend a row on the person doing the searching.

    Email addresses, preferences and privacy settings do not appear here
    because they are not on the shape this returns; there is nothing to
    filter out.
    """
    results = await service.search(q, limit=limit, cursor=cursor, viewer_id=user.id)

    return build_response(
        CursorPage(
            # Composed at the edge from the same schema the profile endpoint
            # renders, so a field added there appears here without this file
            # changing — and one removed cannot linger here.
            items=[
                ProfileResponse.of(profile, avatar_links.links_for(profile.identity.avatar))
                for profile in results.profiles
            ],
            page=CursorPageInfo(
                next_cursor=results.next_cursor,
                has_more=results.has_more,
            ),
        )
    )
