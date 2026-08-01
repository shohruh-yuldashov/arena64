"""`matchmaking` — finding an opponent.

architecture.md §6 names two aggregate roots for this bounded context:
`QueueTicket` and `Challenge`. A64-014.1 builds the first and nothing else.

    domain/          `QueueTicket`, its four states, its three events
    application/     `QueueService` and the ports it declares
    infrastructure/  the `matchmaking` schema, the SKIP LOCKED claim, the
                     provisional rating provider, the expiry task
    presentation/    three endpoints and the composition root
    public/          empty — nothing consumes this module yet

## What this module deliberately does not do

No pairing, no rating-window expansion, no acceptance flow, no match
creation and no realtime updates. Every one of those is excluded by
A64-014.1 and every one of them is a *consumer* of what is here rather than
a change to it: pairing scans `queue_snapshot`, claims through `claim_due`,
and creates a match through the `matchmaking -> game` port architecture.md
§7 already draws.

## The one place this module differs from its design documents

`QueueTicket` is **PostgreSQL-authoritative**, where database.md §8.1 and
domain-model.md row 17 both say Redis. A64-014.1 requires the table; the
argument for it — QT-4's atomic claim and QT-1's uniqueness are both
constraints only a database can hold — is recorded in
`infrastructure/models.py` and in those two documents, which were changed in
the same task (CLAUDE.md §3.11).
"""
