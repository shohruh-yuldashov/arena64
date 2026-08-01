"""`matchmaking`'s HTTP surface and its composition root.

    router.py       three endpoints, no business logic
    schemas/        the wire shapes
    rate_limits.py  this module's policy over the platform's mechanism
    dependencies/   where the object graph is assembled per request

This layer imports `application/` and never `domain/` entities directly or
`infrastructure/` — with the one documented exception every module makes:
`dependencies/` is the composition root and therefore names concrete
classes, which is what `.importlinter`'s privacy contracts leave outside
their source lists (see `apps/api/.importlinter`).
"""
