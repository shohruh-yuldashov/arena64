# `generated/`

`schema.d.ts` is produced by `openapi-typescript` from the backend's own
OpenAPI document. **Never edit it, and never hand-write a type it already
describes** — a hand-copied DTO is a contract that drifts silently and is
discovered by a user.

## Regenerating

```sh
# against a running API
npm run openapi:generate

# against a spec dumped from the FastAPI app, no server needed
cd ../api && uv run python -c \
  "import json; from app.app_factory import create_app; print(json.dumps(create_app().openapi()))" \
  > /tmp/openapi.json
cd ../web && ARENA64_OPENAPI=/tmp/openapi.json npm run openapi:generate
```

## Why it is committed

So `npm run typecheck` and `npm run build` work with no API running and no
network. A generated artefact that is not committed is a build step every
contributor and every CI job has to remember, and the failure mode is a
type error that only appears on the machine that forgot.

## What it does not cover

The response **envelope** (`{data, meta}`) and the error body are produced
by `app/core/responses.py` and an exception handler rather than declared
per-route, so they have no stable generated name. Those four types are
hand-written in `../types.ts`, which says so.
