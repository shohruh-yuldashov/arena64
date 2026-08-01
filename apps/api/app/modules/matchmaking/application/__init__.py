"""`matchmaking`'s use cases and the ports they declare.

    ports.py     `QueueRepository`, `RatingSnapshotProvider`
    services/    `QueueService` — join, leave, read, expire

AD-06: the interfaces live here, in the layer that needs them, so
infrastructure satisfies a contract the use case owns rather than the use
case depending on an adapter.

This layer may import `app.modules.users.public` and no other module's
anything (R-1) — `.importlinter` fails if that changes.
"""
