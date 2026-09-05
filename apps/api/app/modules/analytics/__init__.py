"""Product measurement — A64-027.2, on the contract A64-027.1 froze.

`docs/01-architecture/analytics.md` is the specification and this module is
the implementation of its collection half. What it does **not** do is decide
anything: the `game` module decides a match completed, `rating` decides a
rating moved, and analytics projects those facts into a store that later
tasks query. A consumer that decided facts would be a second source of truth
for them.

Two paths in, and they never carry the same event name:

    a domain fact  ->  outbox  ->  AnalyticsProjector  ->  the event store
    a browser      ->  POST /analytics/events         ->  the event store

The first is authoritative and durable; the second is behavioural and
best-effort. §8 of the document is why that asymmetry is deliberate.
"""
