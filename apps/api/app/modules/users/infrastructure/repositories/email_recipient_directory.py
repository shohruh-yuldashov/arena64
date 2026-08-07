"""The adapter behind `users.public.EmailRecipientDirectory` — A64-021.5 §5.

Database-only, per repositories.md §2. One statement, and the eligibility
policy lives **in the `WHERE` clause** rather than in Python.

That placement is the design. A read that returned every account and let the
caller filter would put the rule "only verified, active addresses" in every
consumer that ever holds this port, and the first one to forget it would
email an unconfirmed address. Here it cannot: the rows that come back are
the rows that qualify, and there is no parameter that relaxes it.
"""

import uuid
from collections.abc import Mapping, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.users.infrastructure.models import UserModel
from app.modules.users.public.email_recipients import EmailRecipient


class SqlAlchemyEmailRecipientDirectory:
    """Constructed per worker pass with that pass's session
    (repositories.md §5.1) — never holds one longer."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def recipients_for(
        self, user_ids: Sequence[uuid.UUID]
    ) -> Mapping[uuid.UUID, EmailRecipient]:
        """Every eligible recipient among these ids. One statement.

        Deduplicated first: a batch can name the same player twice — two
        tournament notifications in one pass — and the `IN` list is the thing
        that grows with a bracket.

        Four columns, not the row. `select(UserModel)` would load a password
        hash and a privacy block into a worker that composes email, and the
        rule that keeps this port narrow should be visible in the statement
        it issues rather than only in its docstring.
        """
        if not user_ids:
            return {}

        rows = (
            await self._session.execute(
                select(
                    UserModel.id,
                    UserModel.email,
                    UserModel.preferred_language,
                    UserModel.display_name,
                    UserModel.username,
                ).where(
                    UserModel.id.in_(set(user_ids)),
                    # The whole eligibility policy, and it is three
                    # predicates rather than a flag a caller reads:
                    #
                    #   is_verified   §6 — an unconfirmed address may belong
                    #                 to somebody who never asked for it, and
                    #                 emailing it is how a platform becomes
                    #                 the thing that spams a typo
                    #   is_active     a deactivated or deleted account is not
                    #                 somebody to write to
                    #   email <> ''   a `CHECK` already forbids it; the
                    #                 predicate is here so the guarantee is
                    #                 the query's rather than the schema's
                    UserModel.is_verified.is_(True),
                    UserModel.is_active.is_(True),
                    UserModel.email != "",
                )
            )
        ).all()

        return {
            row.id: EmailRecipient(
                user_id=row.id,
                email=row.email,
                locale=row.preferred_language,
                # The same fallback every surface on this platform applies.
                # Never empty, so a greeting cannot render a bare comma.
                display_name=row.display_name or row.username,
            )
            for row in rows
        }


__all__ = ["SqlAlchemyEmailRecipientDirectory"]
