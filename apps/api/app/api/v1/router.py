"""Version 1 of the platform's HTTP surface.

**On DI-04 and this file.** A64-006 wrote that a module "registers itself;
this file is never edited to add one", pointing at
`app.core.module_registry`. A64-010 mounts `users` here explicitly instead,
and the reason is worth stating rather than quietly reversing:

DI-04's actual value is that a module owns *its own DI bindings*, so
adding one never means editing another module's code or a shared wiring
file full of service registrations. That value is real. But `users` has no
bindings to own — its entire object graph is assembled by FastAPI
`Depends` at the presentation layer (DI-01), which is resolved per request
and never passes through a container. Routing it through the registry
would mean a `Module` class whose `configure()` method is empty, existing
only to satisfy the shape, and `app_factory` iterating a registry of one to
reach a router it could have imported directly. That is ceremony, not
decoupling — and the enumeration does not even disappear, it moves.

The registry earns its place at the first module that needs bindings
`Depends` cannot express: a Celery task, a gateway handler, or a port
bound differently per profile (dependency-injection.md §1.5). None exists
yet. Until then this is one honest, greppable line, and the trade is
recorded here rather than discovered later as an unexplained deviation.
"""

from fastapi import APIRouter

from app.api.v1.health import health_router
from app.core.constants import API_V1_PREFIX
from app.modules.auth.presentation.browser_router import browser_auth_router
from app.modules.auth.presentation.router import auth_router
from app.modules.avatars.presentation.router import avatar_router
from app.modules.friends.presentation.router import blocks_router, friends_router
from app.modules.game.presentation.router import history_router, replay_router
from app.modules.matchmaking.presentation.router import matchmaking_router
from app.modules.notifications.presentation.router import notifications_router
from app.modules.profiles.presentation.router import profiles_router
from app.modules.profiles.presentation.search_router import user_search_router
from app.modules.profiles.presentation.self_router import my_profile_router
from app.modules.rating.presentation.router import leaderboard_router, ratings_router
from app.modules.reference.presentation.router import time_controls_router
from app.modules.tournament.presentation.router import (
    player_tournaments_router,
    tournaments_router,
)
from app.modules.users.presentation.router import users_router

v1_router = APIRouter(prefix=API_V1_PREFIX)
v1_router.include_router(health_router)

# **Before `users_router`, and the order is load-bearing.** `GET /users/search`
# and `GET /users/{user_id}` both match the path `/users/search`; Starlette
# resolves in registration order, so registering the parameterised route first
# would make every search a `422` complaining that `search` is not a UUID.
#
# The two live in different modules — the search composes a public profile and
# therefore belongs to `profiles` — so this ordering cannot be enforced by
# their decorators, only here. `tests/contract/test_user_search_api.py` asserts
# the resolution rather than trusting this comment.
v1_router.include_router(user_search_router)
v1_router.include_router(users_router)
v1_router.include_router(auth_router)

# A64-020.2. **Before** nothing and after nothing in particular: every path
# here starts `/auth/browser/`, which no other router claims. Registered as
# its own router rather than as routes on `auth_router` so that the two
# surfaces — JSON for native clients, cookies for browsers — are separable
# in the route table and in the OpenAPI document.
v1_router.include_router(browser_auth_router)
v1_router.include_router(profiles_router)
v1_router.include_router(my_profile_router)
v1_router.include_router(avatar_router)
v1_router.include_router(friends_router)
v1_router.include_router(blocks_router)

# A64-014.1. Registration order is immaterial here: `/matchmaking/queue` and
# `/matchmaking/queue/me` differ in segment count and neither is
# parameterised, so there is no path a caller can send that both would match.
v1_router.include_router(matchmaking_router)

# A64-018.3. Two prefixes rather than one: a player's history hangs off
# `/players`, a replay off `/matches`, and neither path is one the other
# could match. `/players/{id}/matches` is parameterised and `/matches/{id}/replay`
# is a different first segment, so registration order is immaterial here too.
v1_router.include_router(history_router)
v1_router.include_router(replay_router)

# A64-020.0A. `/ratings/me` and `/players/{id}/ratings` are unparameterised
# and parameterised respectively but differ in their first segment, and
# `/leaderboard` and `/leaderboard/around/{id}` differ in segment count — so
# no path a caller can send matches two of these.
v1_router.include_router(ratings_router)
v1_router.include_router(leaderboard_router)

# A64-019.6. Two prefixes, and registration order is immaterial for both:
# `/tournaments/{id}`, `/tournaments/{id}/bracket` and
# `/tournaments/{id}/standings` differ in segment count, and
# `/players/{id}/tournaments` differs from `/players/{id}/matches` in its
# last segment. No path a caller can send matches two of them.
v1_router.include_router(tournaments_router)
v1_router.include_router(player_tournaments_router)

# A64-021.1. `/notifications`, `/notifications/unread-count`,
# `/notifications/read-all` and `/notifications/{id}/read` differ in segment
# count or in a literal segment, so no path a client can send matches two of
# them — registration order is immaterial here.
#
# A64-021.3 adds `/notifications/preferences` on GET and PATCH. It is one
# segment where `/{id}/read` is two, and its methods are ones no other route
# under this prefix uses, so the same property holds.
v1_router.include_router(notifications_router)

# A64-020.5A. `/time-controls` collides with nothing — it is a single
# unparameterised segment that no other router claims — so its position here
# is alphabetical rather than load-bearing.
v1_router.include_router(time_controls_router)
