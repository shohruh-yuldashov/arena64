"""Platform infrastructure that belongs to no bounded context.

The first inhabitant is the transactional outbox (AD-16), and it is what
forced this package to exist. database.md §232 assigns `outbox` and
`processed_event` to the **`platform` schema, owned by *(platform)*** — not
to `friends`, not to `notifications`, and not to any module. That ownership
is load-bearing rather than clerical: `game` will emit a match-completed
event, `ratings` will emit an adjustment, `chat` will emit a message. If the
outbox lived inside the module that happens to be its first producer, every
one of those would have to import that module in order to publish.

So the rule is the same one `app/core` follows: **anything here may be
imported by any module; nothing here may import a module.** A grep for
`app.modules` under this package must return nothing, and the day it does
not, this package has become a module with a misleading name.

`app/core` versus `app/platform`, since both are module-agnostic: `core`
holds contracts and primitives with no storage of their own — a `Clock`, a
`UnitOfWork` protocol, error codes. This holds infrastructure that owns
*relations and processes* — a table, a repository, a background worker.
"""
