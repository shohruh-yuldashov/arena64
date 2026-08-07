"""An in-memory `VerificationTokenRepository` — repositories.md RP-05's
fake.

Held to the same contract suite as the SQLAlchemy adapter
(`tests/contract/test_verification_token_repository.py`), which is the
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
from app.modules.auth.domain.verification import EmailVerificationToken


class FakeVerificationTokenRepository:
    def __init__(self, tokens: list[EmailVerificationToken] | None = None) -> None:
        self._tokens: dict[UUID, EmailVerificationToken] = {}
        for token in tokens or []:
            self._tokens[token.id] = copy.deepcopy(token)

    async def create(self, token: EmailVerificationToken) -> EmailVerificationToken:
        # Mirrors `uq_email_verification_tokens__token_hash`.
        if any(existing.token_hash == token.token_hash for existing in self._tokens.values()):
            raise ConflictError("Could not issue a verification token.")

        # Mirrors `uq_email_verification_tokens__one_live_per_user`.
        if any(
            existing.user_id == token.user_id and not existing.is_used
            for existing in self._tokens.values()
        ):
            raise ConflictError("Could not issue a verification token.")

        self._tokens[token.id] = copy.deepcopy(token)
        return copy.deepcopy(token)

    async def get_by_hash(self, token_hash: bytes) -> EmailVerificationToken | None:
        for token in self._tokens.values():
            if token.token_hash == token_hash:
                return copy.deepcopy(token)
        return None

    async def live_for_user(self, user_id: UUID, *, at: datetime) -> EmailVerificationToken | None:
        """A64-021.5H. Relies on the same one-live-per-user rule `create`
        enforces above, so there is never a second candidate to choose
        between — and a fake that returned an arbitrary one of two would
        hide exactly the bug that invariant exists to prevent."""
        for token in self._tokens.values():
            if token.user_id == user_id and not token.is_used:
                return copy.deepcopy(token)
        return None

    async def record_failed_attempt(self, challenge_id: UUID) -> int:
        """A64-021.5H. Mutates the **stored** token and returns the new
        total, matching the real adapter's `UPDATE ... RETURNING`.

        Deliberately not `copy`-then-store: the point of this method is
        that the count survives, and a fake that incremented a copy would
        let a service test spend an unlimited number of guesses.
        """
        token = self._tokens.get(challenge_id)
        if token is None:
            raise LookupError(f"no verification challenge {challenge_id}")
        token.attempt_count += 1
        return token.attempt_count

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
