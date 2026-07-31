"""In-memory fakes for repository ports — repositories.md RP-05.

Application-layer tests must run without a database ("if they need
Postgres, they will be slow, and slow tests stop being run"). These are
what makes that possible. Every one is held to the *same* contract suite
as its real adapter (`tests/contract/`), because a fake that has quietly
diverged produces the worst possible outcome: a green suite over broken
behaviour.
"""
