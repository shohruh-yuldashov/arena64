"""The challenge routes are published by the **production** app graph.

## Why this file exists

A manual browser test found `POST /api/v1/challenges` returning `404` after
the Friend Challenges epic had shipped, been audited, and been declared
production-ready. The route was fine; the developer's API process had been
running since before the routes existed, and `uvicorn` without `--reload`
serves the route table it was born with.

The stale process is an *operational* fact and no test can assert it. What
this asserts is the thing that made the symptom hard to place: **nothing in
the repository checked that the real composition root exposes these paths.**
`tests/contract/test_challenge_api.py` drives them through a contract app
assembled for tests, which proves the router works and says nothing about
whether `app/api/v1/router.py` still includes it.

So this is the smallest test that would have made the diagnosis immediate —
"the app you built from this source *does* serve it, so the one answering
you is not this source" — and it is a genuine regression guard for the
future: an `include_router` line deleted in a refactor fails here rather
than in somebody's browser.

`create_app()` and its `openapi()`, exactly as `test_auth_api_contract.py`
does it. No database, no client, no fixtures.
"""

from typing import Any

import pytest

from app.app_factory import create_app

#: Every path the friend challenge surface publishes — A64-022.2 §16.
#:
#: Written out rather than derived from the router, because a test that
#: asked the router what it contained would agree with it whatever it
#: contained. These are the URLs the frontend's `features/challenges/api`
#: calls, transcribed from the other side of the boundary.
CHALLENGE_PATHS = (
    "/api/v1/challenges",
    "/api/v1/challenges/incoming",
    "/api/v1/challenges/outgoing",
    "/api/v1/challenges/{challenge_id}",
    "/api/v1/challenges/{challenge_id}/accept",
    "/api/v1/challenges/{challenge_id}/decline",
)


@pytest.fixture(scope="module")
def spec() -> dict[str, Any]:
    return create_app().openapi()


def test_every_challenge_path_is_published(spec: dict[str, Any]) -> None:
    missing = [path for path in CHALLENGE_PATHS if path not in spec["paths"]]
    assert not missing, f"the production app does not publish {missing}"


def test_the_create_route_accepts_the_body_the_client_sends(spec: dict[str, Any]) -> None:
    """The one request the browser makes to start the flow.

    Method and body together: a `POST` that existed but required a field the
    client does not send would be a `422` rather than a `404`, and would
    look like a different bug entirely.
    """
    create = spec["paths"]["/api/v1/challenges"]["post"]
    schema_name = create["requestBody"]["content"]["application/json"]["schema"]["$ref"]
    assert schema_name.endswith("/CreateChallengeRequest")

    required = set(spec["components"]["schemas"]["CreateChallengeRequest"]["required"])
    # Exactly what `createChallenge` sends. `variant` and `rated` carry
    # defaults on the server and are sent anyway; `recipient_id` and
    # `time_control_id` are the two a caller must supply.
    assert {"recipient_id", "time_control_id"} <= required
    assert "challenger_id" not in spec["components"]["schemas"]["CreateChallengeRequest"].get(
        "properties", {}
    ), "the actor comes from the session and must not be a field"
