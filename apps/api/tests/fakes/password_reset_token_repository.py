"""An in-memory `PasswordResetTokenRepository` — repositories.md RP-05's
fake.

Held to the same contract suite as the SQLAlchemy adapter
(`tests/contract/test_password_reset_token_repository.py`), which is the
only thing keeping the two honest about each other.

The behaviours it goes out of its way to reproduce are the ones a naive
dict would get wrong:

  - **at most one live token per user**, which the real adapter gets from
    a partial unique index. A fake without it would let a service test
    pass while production rejected the insert;
  - **"first consumption wins"** in `invalidate_active_for_user`, matching
    the real `WHERE used_at IS NULL`, so the returned counts agree;
  - **copies on the way in and out**, because the real repository maps
    through an ORM row and hands back a *different object*. A fake storing
    the caller's instance would let a test mutate a "persisted" token
    without saving and still see the change.
"""

import copy
from datetime import datetime
from uuid import UUID

from app.core.exceptions import ConflictError
from app.modules.auth.domain.password_reset import PasswordResetToken


class FakePasswordResetTokenRepository:
    def __init__(self, tokens: list[PasswordResetToken] | None = None) -> None:
        self._tokens: dict[UUID, PasswordResetToken] = {}
        for token in tokens or []:
            self._tokens[token.id] = copy.deepcopy(token)

    async def create(self, token: PasswordResetToken) -> PasswordResetToken:
        # Mirrors `uq_password_reset_tokens__token_hash`.
        if any(existing.token_hash == token.token_hash for existing in self._tokens.values()):
            raise ConflictError("Could not issue a password reset token.")

        # Mirrors `uq_password_reset_tokens__one_live_per_user`.
        if any(
            existing.user_id == token.user_id and not existing.is_used
            for existing in self._tokens.values()
        ):
            raise ConflictError("Could not issue a password reset token.")

        self._tokens[token.id] = copy.deepcopy(token)
        return copy.deepcopy(token)

    async def get_by_hash(self, token_hash: bytes) -> PasswordResetToken | None:
        for token in self._tokens.values():
            if token.token_hash == token_hash:
                return copy.deepcopy(token)
        return None

    async def invalidate_active_for_user(self, user_id: UUID, *, at: datetime) -> int:
        invalidated = 0
        for token in self._tokens.values():
            if token.user_id != user_id or token.is_used:
                continue
            token.consume(at)
            invalidated += 1
        return invalidated

    async def count_active_for_user(self, user_id: UUID, *, at: datetime) -> int:
        return sum(
            1
            for token in self._tokens.values()
            if token.user_id == user_id and token.is_usable_at(at)
        )
