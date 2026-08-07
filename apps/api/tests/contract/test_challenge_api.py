"""The friend challenge API, end to end — A64-022.2 §28, §29.

Through the **production application**: the real router, the real
`Depends` graph, the real `ChallengeService`, the real outbox publisher and
real PostgreSQL. No `dependency_overrides` anywhere — only the database
session is redirected into the test's rolled-back transaction.

That is the reachability proof (§29). A64-022.1 could only assert the
service through its builder because there was no HTTP surface; this phase has
one, and a test that called the service directly would prove the same thing
twice while proving nothing about whether a request reaches it.

## What is asserted, and what deliberately is not

The **authorization boundary** and the **lifecycle**, because those are what
an API adds over a service: who may call what, what a stranger sees, and that
one transition emits exactly one event.

Not the response's field list. A schema test would restate the schema; what
matters is that the wrong people cannot act and that nothing private travels,
and both are asserted directly.

Skipped, not failed, when PostgreSQL is unreachable (see `conftest.py`).
"""

from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID, uuid4

import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.game.infrastructure.models import MatchRecordModel
from app.platform.outbox.models import OutboxModel
from tests.contract.contract_app import build_contract_app, contract_client

CHALLENGES_URL = "/api/v1/challenges"
INCOMING_URL = f"{CHALLENGES_URL}/incoming"
OUTGOING_URL = f"{CHALLENGES_URL}/outgoing"
REGISTER_URL = "/api/v1/auth/register"
LOGIN_URL = "/api/v1/auth/login"
PASSWORD = "CorrectHorse1!"

CLOCK = "blitz_3_2"


class Player:
    def __init__(self, player_id: UUID, auth: dict[str, str]) -> None:
        self.id = player_id
        self.auth = auth


@pytest_asyncio.fixture
async def client(contract_session: AsyncSession) -> AsyncIterator[AsyncClient]:
    """The production app over the test's rolled-back transaction."""
    async with contract_client(build_contract_app(contract_session)) as http:
        yield http


async def register(client: AsyncClient, session: AsyncSession) -> Player:
    """One verified account with a session.

    Verified because every write here is `VerifiedUser` — the same thing
    `app.operator.accounts verify` does, rather than driving the OTP flow,
    which belongs to its own suite.
    """
    suffix = uuid4().hex[:10]
    created = await client.post(
        REGISTER_URL,
        json={"username": f"ch{suffix}", "email": f"{suffix}@example.com", "password": PASSWORD},
    )
    assert created.status_code == 201, created.text
    player_id = UUID(created.json()["data"]["id"])

    await session.execute(
        text("UPDATE users.user SET is_verified = true WHERE id = :id"), {"id": player_id}
    )
    signed_in = await client.post(
        LOGIN_URL, json={"email": f"{suffix}@example.com", "password": PASSWORD}
    )
    assert signed_in.status_code == 200, signed_in.text
    token = signed_in.json()["data"]["access_token"]
    return Player(player_id, {"Authorization": f"Bearer {token}"})


async def befriend(session: AsyncSession, a: UUID, b: UUID) -> None:
    low, high = sorted((a, b), key=str)
    await session.execute(
        text(
            "INSERT INTO friends.friendship (id, player_low_id, player_high_id, created_at) "
            "VALUES (:id, :low, :high, now())"
        ),
        {"id": uuid4(), "low": low, "high": high},
    )


async def friends_pair(client: AsyncClient, session: AsyncSession) -> tuple[Player, Player]:
    first, second = await register(client, session), await register(client, session)
    await befriend(session, first.id, second.id)
    return first, second


async def challenge(
    client: AsyncClient, sender: Player, recipient: Player, *, rated: bool = False
) -> dict[str, Any]:
    sent = await client.post(
        CHALLENGES_URL,
        headers=sender.auth,
        json={"recipient_id": str(recipient.id), "time_control_id": CLOCK, "rated": rated},
    )
    assert sent.status_code == 201, sent.text
    body: dict[str, Any] = sent.json()["data"]
    return body


async def events_of(session: AsyncSession, event_type: str) -> list[dict[str, Any]]:
    """Every outbox entry of one type, as the relay would read them."""
    rows = await session.scalars(select(OutboxModel).where(OutboxModel.event_type == event_type))
    return [dict(row.payload) for row in rows]


async def match_row(session: AsyncSession, match_id: UUID) -> MatchRecordModel | None:
    row: MatchRecordModel | None = await session.scalar(
        select(MatchRecordModel).where(MatchRecordModel.id == match_id)
    )
    return row


async def match_count(session: AsyncSession) -> int:
    """How many matches exist at all.

    The suite runs in a rolled-back transaction of its own, so "at all" is
    "created by this test" — which is what makes `== 0` a meaningful
    assertion about a refusal.
    """
    return len((await session.scalars(select(MatchRecordModel.id))).all())


async def status_of(session: AsyncSession, challenge_id: UUID) -> str:
    row = await session.scalar(
        text("SELECT status FROM matchmaking.friend_challenge WHERE id = :id").bindparams(
            id=challenge_id
        )
    )
    return str(row)


class TestCreating:
    async def test_a_verified_friend_may_challenge_and_the_event_is_staged(
        self, client: AsyncClient, contract_session: AsyncSession
    ) -> None:
        """The happy path, plus the assertion this phase exists for.

        A64-022.1 wrote the row and published nothing. What is new here is
        that the **outbox carries the fact** — in the same transaction, so a
        challenge that committed without its event cannot exist — and that the
        payload is the durable one: ids and settings, no prose, no names.
        """
        challenger, recipient = await friends_pair(client, contract_session)

        created = await challenge(client, challenger, recipient)

        assert created["status"] == "pending"
        # The other party's profile, composed through `profiles`.
        assert created["player"]["id"] == str(recipient.id)
        assert created["created_match_id"] is None

        staged = await events_of(contract_session, "matchmaking.friend_challenge_created")
        assert len(staged) == 1
        assert staged[0]["challenger_id"] == str(challenger.id)
        assert staged[0]["recipient_id"] == str(recipient.id)
        assert staged[0]["time_control_id"] == CLOCK
        # Durable facts only — a name here would be presentation in an event.
        assert "username" not in staged[0]
        assert "display_name" not in staged[0]

    async def test_a_stranger_is_refused_without_saying_why(
        self, client: AsyncClient, contract_session: AsyncSession
    ) -> None:
        """§25. Not friends and blocked answer identically, so the refusal
        can say neither — and the code must not distinguish them either."""
        challenger = await register(client, contract_session)
        stranger = await register(client, contract_session)

        refused = await client.post(
            CHALLENGES_URL,
            headers=challenger.auth,
            json={"recipient_id": str(stranger.id), "time_control_id": CLOCK},
        )

        assert refused.status_code == 422
        assert refused.json()["code"] == "challenge_not_friends"
        assert await events_of(contract_session, "matchmaking.friend_challenge_created") == []

    async def test_a_supplied_challenger_id_is_refused(
        self, client: AsyncClient, contract_session: AsyncSession
    ) -> None:
        """§4: the actor comes from the session and there is no field to
        override it with.

        `extra="forbid"` makes the attempt a `422` rather than a silently
        ignored field — a client that thought it was challenging on somebody
        else's behalf should be told it was not, and a test asserting only
        that the *effect* was absent would pass against a model that quietly
        dropped it.
        """
        challenger, recipient = await friends_pair(client, contract_session)
        victim = await register(client, contract_session)

        refused = await client.post(
            CHALLENGES_URL,
            headers=challenger.auth,
            json={
                "recipient_id": str(recipient.id),
                "time_control_id": CLOCK,
                "challenger_id": str(victim.id),
            },
        )

        assert refused.status_code == 422


class TestReading:
    async def test_incoming_and_outgoing_are_scoped_to_the_caller(
        self, client: AsyncClient, contract_session: AsyncSession
    ) -> None:
        """The same challenge appears in exactly one list for each party, and
        the profile shown is always the *other* person."""
        challenger, recipient = await friends_pair(client, contract_session)
        created = await challenge(client, challenger, recipient)

        incoming = (await client.get(INCOMING_URL, headers=recipient.auth)).json()["data"]
        outgoing = (await client.get(OUTGOING_URL, headers=challenger.auth)).json()["data"]

        assert [row["id"] for row in incoming["items"]] == [created["id"]]
        assert incoming["items"][0]["player"]["id"] == str(challenger.id)
        assert [row["id"] for row in outgoing["items"]] == [created["id"]]
        assert outgoing["items"][0]["player"]["id"] == str(recipient.id)

        # And neither list shows the other direction.
        assert (await client.get(OUTGOING_URL, headers=recipient.auth)).json()["data"][
            "items"
        ] == []

    async def test_a_page_boundary_neither_skips_nor_repeats(
        self, client: AsyncClient, contract_session: AsyncSession
    ) -> None:
        """Keyset pagination, over rows that share a `created_at` closely
        enough to matter.

        Three challengers rather than three challenges from one person: the
        live-pair rule permits one challenge per pair, so a page of three
        needs three pairs. That is also the realistic shape — a list of
        invitations is a list of *people*.
        """
        recipient = await register(client, contract_session)
        senders = []
        for _ in range(3):
            sender = await register(client, contract_session)
            await befriend(contract_session, sender.id, recipient.id)
            await challenge(client, sender, recipient)
            senders.append(sender)

        first = (
            await client.get(INCOMING_URL, headers=recipient.auth, params={"limit": 2})
        ).json()["data"]
        assert len(first["items"]) == 2
        assert first["page"]["has_more"] is True

        second = (
            await client.get(
                INCOMING_URL,
                headers=recipient.auth,
                params={"limit": 2, "cursor": first["page"]["next_cursor"]},
            )
        ).json()["data"]

        assert len(second["items"]) == 1
        assert second["page"]["has_more"] is False
        seen = [row["id"] for row in first["items"]] + [row["id"] for row in second["items"]]
        assert len(set(seen)) == 3

    async def test_a_stranger_gets_not_found_rather_than_forbidden(
        self, client: AsyncClient, contract_session: AsyncSession
    ) -> None:
        """§25's IDOR rule, at the HTTP boundary.

        A `403` here would confirm the identifier names a real challenge,
        which is exactly what a caller probing UUIDs is trying to learn.
        """
        challenger, recipient = await friends_pair(client, contract_session)
        outsider = await register(client, contract_session)
        created = await challenge(client, challenger, recipient)

        answered = await client.get(f"{CHALLENGES_URL}/{created['id']}", headers=outsider.auth)

        assert answered.status_code == 404


class TestAnswering:
    async def test_the_recipient_declines_and_exactly_one_event_is_staged(
        self, client: AsyncClient, contract_session: AsyncSession
    ) -> None:
        """§12: an event per transition that actually happened.

        The second decline is refused, and the assertion that matters is that
        it stages **no second event** — a duplicate that reported success
        would announce one refusal twice to everything downstream.
        """
        challenger, recipient = await friends_pair(client, contract_session)
        created = await challenge(client, challenger, recipient)

        declined = await client.post(
            f"{CHALLENGES_URL}/{created['id']}/decline", headers=recipient.auth
        )
        again = await client.post(
            f"{CHALLENGES_URL}/{created['id']}/decline", headers=recipient.auth
        )

        assert declined.status_code == 200
        assert declined.json()["data"]["status"] == "declined"
        assert again.status_code == 422
        assert again.json()["code"] == "challenge_not_pending"
        assert len(await events_of(contract_session, "matchmaking.friend_challenge_declined")) == 1

    async def test_the_challenger_cancels_and_exactly_one_event_is_staged(
        self, client: AsyncClient, contract_session: AsyncSession
    ) -> None:
        challenger, recipient = await friends_pair(client, contract_session)
        created = await challenge(client, challenger, recipient)

        cancelled = await client.request(
            "DELETE", f"{CHALLENGES_URL}/{created['id']}", headers=challenger.auth
        )

        assert cancelled.status_code == 200
        assert cancelled.json()["data"]["status"] == "cancelled"
        assert len(await events_of(contract_session, "matchmaking.friend_challenge_cancelled")) == 1

    async def test_neither_party_may_use_the_other_s_verb(
        self, client: AsyncClient, contract_session: AsyncSession
    ) -> None:
        """`403`, not `404`: both are parties and both know the challenge
        exists, so hiding it would be a fiction rather than a protection.

        A challenger who could decline would be cancelling under a name that
        reads differently in a history, and a recipient who could cancel
        would be declining without the challenger being told they declined.
        """
        challenger, recipient = await friends_pair(client, contract_session)
        created = await challenge(client, challenger, recipient)

        wrong_decline = await client.post(
            f"{CHALLENGES_URL}/{created['id']}/decline", headers=challenger.auth
        )
        wrong_cancel = await client.request(
            "DELETE", f"{CHALLENGES_URL}/{created['id']}", headers=recipient.auth
        )

        assert wrong_decline.status_code == 403
        assert wrong_cancel.status_code == 403
        assert await events_of(contract_session, "matchmaking.friend_challenge_declined") == []


class TestAccepting:
    """Acceptance, and the invariant the whole phase exists for — §1.

    Never an accepted challenge without a match, never a match without an
    accepted challenge, never two matches for one challenge.
    """

    async def test_accepting_creates_exactly_one_match_with_the_stored_settings(
        self, client: AsyncClient, contract_session: AsyncSession
    ) -> None:
        """The whole operation, asserted on **both** rows.

        The settings are the ones the *challenger* chose — the recipient
        agreed to a proposal and had no way to alter it, because the accept
        request carries no body at all.
        """
        challenger, recipient = await friends_pair(client, contract_session)
        created = await challenge(client, challenger, recipient, rated=True)

        accepted = await client.post(
            f"{CHALLENGES_URL}/{created['id']}/accept", headers=recipient.auth
        )

        assert accepted.status_code == 200, accepted.text
        body = accepted.json()["data"]
        assert body["status"] == "accepted"
        match_id = UUID(body["created_match_id"])

        match = await match_row(contract_session, match_id)
        assert match is not None
        assert match.variant.value == "russian_8x8"
        assert match.rated is True
        # The challenge's own settings, never the request's.
        assert match.origin.value == "challenge"
        assert match.origin_ref == UUID(created["id"])
        # Exactly one, and its identity is derived — so a retry cannot make a
        # second.
        assert await match_count(contract_session) == 1

    async def test_the_two_seats_are_the_two_players_and_neither_is_predictable(
        self, client: AsyncClient, contract_session: AsyncSession
    ) -> None:
        """§9. Both players are seated, and the challenger is **not** always
        light — that would hand a measurable edge to whoever sends the
        invitation, in rated games, forever.

        Asserted as a set rather than by position: which of them is light is
        the parity of a derived id, which this test must not restate.
        """
        challenger, recipient = await friends_pair(client, contract_session)
        created = await challenge(client, challenger, recipient)

        accepted = await client.post(
            f"{CHALLENGES_URL}/{created['id']}/accept", headers=recipient.auth
        )

        match = await match_row(contract_session, UUID(accepted.json()["data"]["created_match_id"]))
        assert match is not None
        assert {match.light_player_id, match.dark_player_id} == {challenger.id, recipient.id}

    async def test_the_challenger_cannot_accept_their_own_challenge(
        self, client: AsyncClient, contract_session: AsyncSession
    ) -> None:
        """`403`, and **no match** — the assertion that matters, because a
        refusal that still created a game would be the worst outcome
        available."""
        challenger, recipient = await friends_pair(client, contract_session)
        created = await challenge(client, challenger, recipient)

        refused = await client.post(
            f"{CHALLENGES_URL}/{created['id']}/accept", headers=challenger.auth
        )

        assert refused.status_code == 403
        assert await match_count(contract_session) == 0

    async def test_an_unfriended_challenge_cannot_be_accepted(
        self, client: AsyncClient, contract_session: AsyncSession
    ) -> None:
        """**§3, and the reason the creation-time check is not enough.**

        A friendship can end in the twenty-four hours between sending and
        answering, and the snapshot is not authority for mutable
        relationship state. Removing the friendship covers blocking too,
        because a block also ends one.

        The challenge stays `pending` and no match exists: the whole
        operation is one transaction, so a failed revalidation leaves
        nothing behind.
        """
        challenger, recipient = await friends_pair(client, contract_session)
        created = await challenge(client, challenger, recipient)
        await contract_session.execute(
            text(
                "DELETE FROM friends.friendship "
                "WHERE player_low_id = :low AND player_high_id = :high"
            ),
            {
                "low": min(challenger.id, recipient.id, key=str),
                "high": max(challenger.id, recipient.id, key=str),
            },
        )

        refused = await client.post(
            f"{CHALLENGES_URL}/{created['id']}/accept", headers=recipient.auth
        )

        assert refused.status_code == 422
        assert refused.json()["code"] == "challenge_not_friends"
        assert await match_count(contract_session) == 0
        assert await status_of(contract_session, UUID(created["id"])) == "pending"

    async def test_a_second_accept_creates_no_second_match(
        self, client: AsyncClient, contract_session: AsyncSession
    ) -> None:
        """§16, §20. Two defences and this proves both hold together.

        The guarded update refuses the second transition, so the second
        request is a bounded conflict rather than a success. And even if it
        were not, `pairing_id` is derived from the challenge id and `game`'s
        unique index admits one match per key — so a second match is
        structurally impossible rather than merely prevented.
        """
        challenger, recipient = await friends_pair(client, contract_session)
        created = await challenge(client, challenger, recipient)

        first = await client.post(
            f"{CHALLENGES_URL}/{created['id']}/accept", headers=recipient.auth
        )
        second = await client.post(
            f"{CHALLENGES_URL}/{created['id']}/accept", headers=recipient.auth
        )

        assert first.status_code == 200
        assert second.status_code == 422
        assert second.json()["code"] == "challenge_not_pending"
        assert await match_count(contract_session) == 1
        assert len(await events_of(contract_session, "matchmaking.friend_challenge_accepted")) == 1

    async def test_cancelling_first_means_no_match_is_ever_created(
        self, client: AsyncClient, contract_session: AsyncSession
    ) -> None:
        """**§17's race, in the order that must not produce a game.**

        `CANCELLED + Match` is the forbidden outcome. The challenge is a
        single row and every transition is guarded on `status = 'pending'`,
        so whichever commits first wins and the loser touches nothing —
        which means acceptance cannot create a match after a cancel landed.
        """
        challenger, recipient = await friends_pair(client, contract_session)
        created = await challenge(client, challenger, recipient)

        await client.request("DELETE", f"{CHALLENGES_URL}/{created['id']}", headers=challenger.auth)
        refused = await client.post(
            f"{CHALLENGES_URL}/{created['id']}/accept", headers=recipient.auth
        )

        assert refused.status_code == 422
        assert await status_of(contract_session, UUID(created["id"])) == "cancelled"
        assert await match_count(contract_session) == 0

    async def test_both_events_are_staged_together(
        self, client: AsyncClient, contract_session: AsyncSession
    ) -> None:
        """§14, §15. One transaction, so a consumer sees both or neither.

        `match.created` is `game`'s own event, published by `game`'s own use
        case — this phase publishes no duplicate of it. Asserting both are in
        the outbox is what proves the two modules committed together rather
        than one succeeding and the other being retried.
        """
        challenger, recipient = await friends_pair(client, contract_session)
        created = await challenge(client, challenger, recipient)

        await client.post(f"{CHALLENGES_URL}/{created['id']}/accept", headers=recipient.auth)

        accepted = await events_of(contract_session, "matchmaking.friend_challenge_accepted")
        matches = await events_of(contract_session, "game.match_created")
        assert len(accepted) == 1
        assert len(matches) == 1
        # The handoff fact, and the one a notification needs.
        assert accepted[0]["match_id"] == matches[0]["match_id"]
        # Durable identifiers only — no prose, no names.
        assert "username" not in accepted[0]


class TestSocialChangesAfterCreation:
    async def test_unfriending_removes_a_live_challenge_from_both_lists(
        self, client: AsyncClient, contract_session: AsyncSession
    ) -> None:
        """**§19, §20.**

        A challenge outlives the friendship that permitted it, because the
        row is the record that an invitation happened and A64-022.1 does not
        delete history. What must not outlive it is the *invitation* — an
        actionable row offering a game with somebody who is no longer a
        friend, or who has blocked you.

        Removing the friendship is the general case: a block also ends the
        friendship, so one mechanism covers both and BL-2 is satisfied
        without this module knowing what a block is.

        The row is still stored, and acceptance re-checks the relationship in
        A64-022.3 — so this is a visibility rule rather than the security
        boundary.
        """
        challenger, recipient = await friends_pair(client, contract_session)
        created = await challenge(client, challenger, recipient)

        await contract_session.execute(
            text(
                "DELETE FROM friends.friendship "
                "WHERE player_low_id = :low AND player_high_id = :high"
            ),
            {
                "low": min(challenger.id, recipient.id, key=str),
                "high": max(challenger.id, recipient.id, key=str),
            },
        )

        incoming = (await client.get(INCOMING_URL, headers=recipient.auth)).json()["data"]
        outgoing = (await client.get(OUTGOING_URL, headers=challenger.auth)).json()["data"]

        assert incoming["items"] == []
        assert outgoing["items"] == []
        # Still readable by id — the record survives, the invitation does not.
        direct = await client.get(f"{CHALLENGES_URL}/{created['id']}", headers=recipient.auth)
        assert direct.status_code == 200
