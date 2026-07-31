"""The only package other modules may import from `auth` — BE-03.

Empty of ports today, deliberately. Nothing else on the platform needs
anything from `auth` yet: `users` is *downstream* of it (auth calls users,
not the reverse), and no module has a reason to ask auth a question until
something needs "is this request authenticated", which arrives with
A64-011.2's sessions.

Publishing a speculative `AuthenticationPort` now would be exactly the
generality CLAUDE.md §1 rule 7 warns against — and a port with no caller
is a port whose shape is guessed rather than derived.
"""

__all__: list[str] = []
