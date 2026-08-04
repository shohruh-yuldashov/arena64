"""`rating` — measured skill, per `(variant, speed class)`.

SPEC-RATING, ADR-001: **Glicko-2**, applied **incrementally** on each
completed rated match, with **no rating periods** and no scheduled writer.

What exists today is the domain core: the key (`domain/keys.py`) and the
arithmetic (`domain/glicko2.py`), both pure and both framework-free. The
aggregate, the persistence and the `match_completed` consumer follow.

`domain-model.md` Q-3 was the platform`s oldest open question and blocked
this module from being built at all; ADR-001 answers it.
"""
