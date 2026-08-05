"""The operator process profile — A64-019.8.

`main.py` runs the HTTP profile and names the others: "the gateway,
worker, and clock profiles are separate entrypoints under the same
distribution (services.md §1)". This is one more of them, and the reason it
exists rather than a set of `/admin` routes is stated once here so no
future module has to rediscover it.

## Why these commands are not HTTP

Creating a tournament, opening and closing its registration, seeding it and
starting it are **administrator** actions (T-3). This platform has no
administrator:

    users.User          is_active, is_verified — and nothing else
    auth.TokenClaims    sub, jti, type, iat, exp, iss, aud — no scope
    settings            no operator credential, no internal API key
    anywhere            no role, no permission, no policy primitive

An `/api/v1/admin/...` route would therefore have to sit behind
`CurrentUser` — which is *every registered player*. That would let anybody
create tournaments, close somebody else's registration and start a
tournament early. Inventing a boolean column to avoid it would be a
half-designed authorization model shipped under time pressure, and
authorization is the one thing this codebase should not guess at.

So the boundary is the **process**, which is a real boundary the deployment
already has: whoever can run a command on the host is already trusted with
the database. When the Administration epic ships a role, these commands
become the thing its routes call — the use cases do not change, only who is
allowed to reach them.

See `specs/tournament/audit.md` §4 and `specs/tournament.md` OQ-5.
"""
