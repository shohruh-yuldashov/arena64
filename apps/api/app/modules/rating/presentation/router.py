"""Ratings and the ladder — A64-020.0A.

Four read-only endpoints. Thin, like every other router here: each resolves
a reader, converts a query parameter and maps a value to a response. No SQL,
no ranking arithmetic, no rating calculation — `rating` applies an
adjustment in response to `game.match_completed` and nowhere else, and there
is deliberately no endpoint on this surface that could request one.

    GET /ratings/me                        the caller's own standings
    GET /players/{id}/ratings               anybody's, publicly
    GET /leaderboard                        one key's ladder, keyset-paged
    GET /leaderboard/around/{player_id}     where one player stands

## Public, and what that means

A rating is a public competitive record — the same rule match history keeps
for rated games. There is no owner check and no privacy variant, so
`/ratings/me` and `/players/{id}/ratings` differ only in *whose* id they
read: the first takes it from `CurrentUser` and cannot be pointed at
somebody else, the second takes it from the path and is the same answer for
every viewer.

Authenticated, like every route outside `/health`. Visible-to-everybody is
not the same as unauthenticated.

## Why the key is two query parameters rather than a path segment

`(ProductVariant, SpeedClass)` is one key with two components (DM-10), and a
client switching speed class is filtering the same resource rather than
navigating to a different one. Both are closed enums, so an unknown value is
a `422` from FastAPI's own validation rather than an empty ladder that looks
like a key nobody plays.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Path, Query, status

from app.api.openapi import error_response
from app.api.responses import build_response
from app.core.exceptions import NotFoundError
from app.core.responses import ApiResponse
from app.modules.auth.presentation.dependencies import CurrentUser
from app.modules.game.public import ProductVariant
from app.modules.rating.infrastructure.repositories.leaderboard_repository import (
    DEFAULT_PAGE_SIZE,
    DEFAULT_SPAN,
    MAX_PAGE_SIZE,
    MAX_SPAN,
)
from app.modules.rating.presentation.dependencies import (
    LeaderboardReaderDep,
    RatingReaderDep,
)
from app.modules.rating.presentation.schemas.leaderboard import (
    LeaderboardNeighbourhoodResponse,
    LeaderboardResponse,
    decode_cursor,
)
from app.modules.rating.presentation.schemas.ratings import (
    PlayerRatingsResponse,
    every_key,
)
from app.modules.rating.public import DEFAULT_SPEED_CLASS, RatingKey, SpeedClass

#: Two prefixes, so each path reads as the resource it is about. A player's
#: ratings hang off `/players`, beside their matches and their tournaments;
#: the ladder is its own noun.
ratings_router = APIRouter(tags=["rating"])
leaderboard_router = APIRouter(prefix="/leaderboard", tags=["leaderboard"])


@ratings_router.get(
    "/ratings/me",
    response_model=ApiResponse[PlayerRatingsResponse],
    status_code=status.HTTP_200_OK,
    summary="Your own ratings",
)
async def my_ratings(
    user: CurrentUser,
    ratings: RatingReaderDep,
    variant: Annotated[ProductVariant, Query(description="Which rule set.")] = (
        ProductVariant.RUSSIAN_8X8
    ),
) -> ApiResponse[PlayerRatingsResponse]:
    """Every speed class, for the authenticated player.

    **One query for all of them** — `ratings_across` exists precisely so a
    page showing several speed classes side by side is not one query per
    class (the N+1 `rating.public` records).

    A class the player has never played is present and marked provisional
    with zero games, because `rating` answers an absent row with
    `RatingSnapshot.unrated()` rather than with nothing. Omitting them would
    push "has this player played blitz?" onto every client.
    """
    keys = every_key(variant.value)
    snapshots = await ratings.ratings_across([user.id], keys=keys)
    return build_response(PlayerRatingsResponse.of(user.id, dict(snapshots), keys=keys))


@ratings_router.get(
    "/players/{player_id}/ratings",
    response_model=ApiResponse[PlayerRatingsResponse],
    status_code=status.HTTP_200_OK,
    summary="A player's ratings",
)
async def player_ratings(
    user: CurrentUser,
    ratings: RatingReaderDep,
    player_id: Annotated[UUID, Path(description="Whose ratings to read.")],
    variant: Annotated[ProductVariant, Query(description="Which rule set.")] = (
        ProductVariant.RUSSIAN_8X8
    ),
) -> ApiResponse[PlayerRatingsResponse]:
    """The same summary, for anybody.

    No `404` for an unknown id, deliberately: `rating` answers every player
    with a snapshot, so "this account does not exist" and "this account has
    never played" are indistinguishable here — and making them
    distinguishable would turn this endpoint into an account-existence
    oracle. Whether a player exists is `users`' question.
    """
    keys = every_key(variant.value)
    snapshots = await ratings.ratings_across([player_id], keys=keys)
    return build_response(PlayerRatingsResponse.of(player_id, dict(snapshots), keys=keys))


@leaderboard_router.get(
    "",
    response_model=ApiResponse[LeaderboardResponse],
    status_code=status.HTTP_200_OK,
    summary="One key's ladder",
    # 422, not 400: `InvalidCursor` is a `ValidationError`, and the platform
    # maps that taxonomy branch to `422` for every malformed input. Declared
    # as what the handler actually returns.
    responses=error_response(422, "The pagination cursor is not valid"),
)
async def leaderboard(
    user: CurrentUser,
    leaderboards: LeaderboardReaderDep,
    variant: Annotated[ProductVariant, Query(description="Which rule set.")] = (
        ProductVariant.RUSSIAN_8X8
    ),
    speed_class: Annotated[SpeedClass, Query(description="Which speed class.")] = (
        DEFAULT_SPEED_CLASS
    ),
    after: Annotated[
        str | None, Query(description="An opaque cursor from a previous page.")
    ] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
) -> ApiResponse[LeaderboardResponse]:
    """Standings for one key, best first, keyset-paginated.

    **Never `OFFSET`.** A ladder moves while it is read, so an offset page
    can show a player twice or skip them entirely as ratings shift between
    requests; a cursor names the *row* it resumed after, which keeps its
    identity when the ratings around it move.

    Ordered `rating DESC, deviation ASC, player_id ASC` — total, so the
    pagination is stable. Provisional players are ranked and shown, never
    hidden (SPEC-RATING §6).
    """
    key = RatingKey(variant=variant, speed_class=speed_class)
    page = await leaderboards.page(key, after=decode_cursor(after) if after else None, limit=limit)
    return build_response(LeaderboardResponse.of(key, page))


@leaderboard_router.get(
    "/around/{player_id}",
    response_model=ApiResponse[LeaderboardNeighbourhoodResponse],
    status_code=status.HTTP_200_OK,
    summary="Where a player stands",
    responses=error_response(404, "This player has no rating in that key"),
)
async def leaderboard_around(
    user: CurrentUser,
    leaderboards: LeaderboardReaderDep,
    player_id: Annotated[UUID, Path(description="Whose position to find.")],
    variant: Annotated[ProductVariant, Query(description="Which rule set.")] = (
        ProductVariant.RUSSIAN_8X8
    ),
    speed_class: Annotated[SpeedClass, Query(description="Which speed class.")] = (
        DEFAULT_SPEED_CLASS
    ),
    span: Annotated[int, Query(ge=1, le=MAX_SPAN)] = DEFAULT_SPAN,
) -> ApiResponse[LeaderboardNeighbourhoodResponse]:
    """One player's rank and the rows either side — the "where am I?" read.

    A client cannot answer this by paging: finding yourself on a ladder of
    any size is a linear scan, and the rank is a property of the whole
    relation rather than of a page.

    A player with **no rating in this key** is a `404`: they are not on this
    ladder, and there is no position to return. That is different from
    `/players/{id}/ratings`, which answers every id — a rating exists for
    everybody, a *ranking* only for those who have one stored.
    """
    key = RatingKey(variant=variant, speed_class=speed_class)
    neighbourhood = await leaderboards.around(player_id, key=key, span=span)
    if neighbourhood is None:
        raise NotFoundError("That player has no rating on this leaderboard.")
    return build_response(LeaderboardNeighbourhoodResponse.of(key, neighbourhood))


__all__ = ["leaderboard_router", "ratings_router"]
