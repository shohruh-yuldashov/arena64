"""The operator surface's guard — A64-028.6 §5, §9.

Two routes are for operators rather than for players: `/metrics` and
`POST /health/drain`. They share one token because they share one audience
and one blast radius, and because two secrets an operator has to keep in
step is how one of them ends up unset.

Kept in its own module so neither route imports the other, and so the
comparison rule lives once: `compare_digest`, never `==`. The input is
attacker-controlled and the comparison is against a secret, which is the
whole of the argument — the cost is nothing and the alternative leaks the
token one character at a time.
"""

import hmac

from app.config.settings import Settings

_BEARER = "Bearer "


def operator_authorised(header: str | None, settings: Settings) -> bool:
    """Whether an `Authorization` header carries the operator token.

    Returns **True** when no token is configured. That is not a hole: a
    production-like tier with the operator surface enabled and no token
    refuses to start (`Settings._guard_production_observability`), so the
    only way to reach this branch is `local`, or an operator who has said
    in configuration that the network is the boundary.
    """
    expected = settings.observability.token
    if expected is None:
        return True
    if header is None or not header.startswith(_BEARER):
        return False
    return hmac.compare_digest(header.removeprefix(_BEARER), expected.get_secret_value())


__all__ = ["operator_authorised"]
