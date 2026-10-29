"""The leaderboard with names on it — A64-033.

    GET /leaderboard/players        one speed class's ladder, optionally one
                                    country's, keyset-paged, with ranks
    GET /leaderboard/players/me     the caller's own place on that ladder

Beside `rating`'s `/leaderboard` rather than replacing it: that route is
the raw ladder of ids and numbers SPEC-LEADERBOARD §7.2 publishes, and
this is the composed one a page renders. Who appears, and why, is
`LeaderboardService`'s docstring.

Authenticated like the ladder it reads: a rating is public to every player,
which is not the same as public to the internet.

## The country filter never touches a preference

`country` is a query parameter and nothing else. It selects a ladder; it is
not stored, it does not change the caller's country, and it has no
relationship to the interface language.
"""

from typing import Annotated

from fastapi import APIRouter, Query, status

from app.api.openapi import error_response
from app.api.responses import build_response
from app.core.responses import ApiResponse
from app.modules.auth.presentation.dependencies import CurrentUser
from app.modules.game.public import ProductVariant
from app.modules.profiles.application.services.leaderboard_service import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
)
from app.modules.profiles.presentation.dependencies import LeaderboardServiceDep
from app.modules.profiles.presentation.schemas.leaderboard import (
    LeaderboardPlayersResponse,
    LeaderboardStandingResponse,
    decode_position,
)
from app.modules.rating.public import DEFAULT_SPEED_CLASS, RatingKey, SpeedClass

leaderboard_players_router = APIRouter(prefix="/leaderboard/players", tags=["leaderboard"])

#: Two letters, either case; upper-cased before use. An unassigned code is
#: not an error — it is a ladder nobody is on, and saying so would turn the
#: route into a list of which codes the platform accepts.
_COUNTRY = Query(
    pattern=r"^[A-Za-z]{2}$",
    description="ISO 3166-1 alpha-2. Omit for the global ladder.",
)


def _country(value: str | None) -> str | None:
    return value.upper() if value is not None else None


@leaderboard_players_router.get(
    "",
    response_model=ApiResponse[LeaderboardPlayersResponse],
    status_code=status.HTTP_200_OK,
    summary="The leaderboard, with players' public names",
    responses=error_response(422, "The pagination cursor is not valid"),
)
async def leaderboard_players(
    user: CurrentUser,
    leaderboard: LeaderboardServiceDep,
    variant: Annotated[ProductVariant, Query(description="Which rule set.")] = (
        ProductVariant.RUSSIAN_8X8
    ),
    speed_class: Annotated[SpeedClass, Query(description="Which speed class.")] = (
        DEFAULT_SPEED_CLASS
    ),
    country: Annotated[str | None, _COUNTRY] = None,
    after: Annotated[str | None, Query(description="An opaque cursor.")] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
) -> ApiResponse[LeaderboardPlayersResponse]:
    """Eligible players, best first, ranked among themselves."""
    key = RatingKey(variant=variant, speed_class=speed_class)
    chosen = _country(country)
    view = await leaderboard.page(
        key,
        country=chosen,
        after=decode_position(after) if after else None,
        limit=limit,
    )
    return build_response(LeaderboardPlayersResponse.of(key, chosen, view))


@leaderboard_players_router.get(
    "/me",
    response_model=ApiResponse[LeaderboardStandingResponse],
    status_code=status.HTTP_200_OK,
    summary="Your own place on the leaderboard",
)
async def my_leaderboard_standing(
    user: CurrentUser,
    leaderboard: LeaderboardServiceDep,
    variant: Annotated[ProductVariant, Query(description="Which rule set.")] = (
        ProductVariant.RUSSIAN_8X8
    ),
    speed_class: Annotated[SpeedClass, Query(description="Which speed class.")] = (
        DEFAULT_SPEED_CLASS
    ),
    country: Annotated[str | None, _COUNTRY] = None,
) -> ApiResponse[LeaderboardStandingResponse]:
    """The caller's place — the player comes from the token, never the request."""
    key = RatingKey(variant=variant, speed_class=speed_class)
    chosen = _country(country)
    standing = await leaderboard.standing(user.id, key, country=chosen)
    return build_response(LeaderboardStandingResponse.of(key, chosen, standing))


__all__ = ["leaderboard_players_router"]
