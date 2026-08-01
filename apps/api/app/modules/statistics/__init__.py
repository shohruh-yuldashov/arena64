"""`statistics` — a player's aggregate competitive record.

A **projection** context (domain-model.md DM-03, §11.5): every number here
is a count over match history, nothing is the system of record for
anything, and the whole thing is rebuildable by definition.

Introduced by A64-012.6 to replace the placeholder `profiles` had been
serving since A64-012.1. What that task built is the *reading* half —
storage, a repository, a service and one published port — because the
writing half is a consumer of `match.completed` and there is no `game`
module to emit one.

`profiles` is a consumer and only a consumer: it reads through
`statistics.public.StatisticsReader` and does not know this schema exists.
"""
