"""The `auth` bounded context — proof of identity.

Owns *credentials and the act of proving who you are*. `users` owns *who
you appear to be* (architecture.md §6 keeps them separate contexts, and
domain-model.md DM-10 explains why: a player can exist without an
account — a bot seat, a guest — so identity and credentials cannot be the
same aggregate).

## What A64-011.1 implements

Registration only: validate a password, hash it with Argon2id, and ask
`users` to create the account. That is the whole module today.

Explicitly **not** here, per the task's constraints: login, JWT, refresh
tokens, sessions, email verification, password reset, OAuth. Each is a
later slice, and none is stubbed — an empty `login()` waiting to be filled
in is worse than its absence, because it reads as "supported" to the next
person.

## The boundary that matters

`auth` hashes; `users` stores. This module never reads or writes the
database — it has no models, no repository and no migration of its own.
It reaches `users` through exactly one published port
(`users.public.UserAccountCreator`), which is the only import of another
module anywhere in here (BR-1).

That split is what makes password policy and hashing cost changeable in
one place. It also means `password_hash` sitting on `users.user` is a
storage location, not an ownership claim — see
`users/infrastructure/models.py` for that documented deviation and the
path to splitting it.

## Layout

Mirrors `users` exactly (`domain` / `application` / `infrastructure` /
`presentation` / `public`), for the reason services.md §2.1 gives:
uniformity is worth more than local optimisation, because a contributor
who has read one module can navigate the next.
"""
