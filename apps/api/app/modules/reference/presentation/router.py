"""The time control catalogue over HTTP — A64-020.5A §3.

One endpoint, and it exists because a lobby cannot be built without it.
A64-020.5A-pre made the catalogue authoritative and reachable from
`matchmaking`; nothing published it to a browser, which left a client with
two options and both wrong: hardcode the four controls, or parse
`base_time_ms` out of an identifier string. Either makes the frontend a
second definition of what "3+2" means, and the first one to drift wins
silently.

    GET /time-controls    every control a player may currently choose

## Why there is no `{id}` route

A client picks from the list it was given, and the server re-validates the
identifier on `POST /matchmaking/queue` — where it matters, because that is
the request that writes a permanent record. A per-id read would answer a
question nobody asks and would invite a client to resolve a control it did
not get from the menu.

## Why it is authenticated

Every route outside `/health` is. There is nothing sensitive here — the
catalogue is the same for everybody and appears in the OpenAPI document —
but "visible to every player" is not the same as "reachable without a
token", and an exception would be the first one.

## Why it is not paginated

CLAUDE.md §10.5 bounds every list endpoint, and this one is bounded by the
*table*: four rows today, and a catalogue that needed a cursor would be a
menu no player could read. `active()` states the same thing at the port.
"""

from fastapi import APIRouter, status

from app.api.openapi import Responses, error_response
from app.api.responses import build_response
from app.core.responses import ApiResponse
from app.modules.auth.presentation.dependencies import CurrentUser
from app.modules.reference.presentation.dependencies import TimeControlCatalogueDep
from app.modules.reference.presentation.schemas import TimeControlResponse

time_controls_router = APIRouter(tags=["reference"])

_UNAUTHORIZED: Responses = error_response(
    401, "No access token was presented, or it was invalid or expired."
)


@time_controls_router.get(
    "/time-controls",
    status_code=status.HTTP_200_OK,
    summary="List the time controls on offer",
    response_description="Every control a player may currently choose, in display order.",
    responses={**_UNAUTHORIZED},
)
async def list_time_controls(
    user: CurrentUser,
    catalogue: TimeControlCatalogueDep,
) -> ApiResponse[list[TimeControlResponse]]:
    """The clocks Arena64 offers.

    **Read this rather than hardcoding it.** A control can be retired, and a
    client holding a stale list will send an identifier that is refused with
    `422 unsupported_time_control` — the honest failure, and one a fresh
    read fixes.

    **Order is part of the contract.** Deterministic, from the catalogue's
    own `display_order`, so a picker renders the same list in the same
    sequence on every device. A player who learned that the second entry is
    3+2 should not find something else there tomorrow.

    Only **active** controls appear. There is no field saying so and no way
    to ask for the retired ones: a menu of things that cannot be chosen is
    not a menu, and the games already played under a withdrawn control keep
    their own recorded settings regardless of what this returns.

    Never empty in a correctly migrated environment — the four controls are
    seeded by the migration that creates the table, so an empty list means
    the deployment is not migrated rather than that Arena64 offers no games.
    """
    offered = await catalogue.active()
    return build_response([TimeControlResponse.of(control) for control in offered])


__all__ = ["time_controls_router"]
