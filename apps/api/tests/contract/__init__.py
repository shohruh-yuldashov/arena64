"""Repository contract suites — one suite run against a port's fake and
every real adapter (repositories.md RP-05) — plus the engine conformance
corpus (architecture.md AD-14).

A64-009 adds the first tenants: the platform's own database
infrastructure (mixins, `BaseRepository`, pagination, the session
manager) tested against a real PostgreSQL 17, per RP-05's "every real
adapter." No module-specific port or fake exists yet — those arrive with
the first module.
"""
