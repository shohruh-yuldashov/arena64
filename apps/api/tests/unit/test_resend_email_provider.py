"""The Resend transport — A64-021.5 continuation §10, §32.

Against a **stubbed HTTP boundary**, never Resend. §8 is explicit that no
automated test contacts a provider, and `httpx.MockTransport` is the
narrowest place to cut: everything above it — the payload, the headers, the
status classification, the id extraction — is the code under test, and only
the socket is replaced.

## What is asserted, and what deliberately is not

The **request** Resend receives, because that is the contract: a sender it
will accept, a recipient, a subject, both bodies, and the idempotency key
that makes a timed-out retry safe.

The **classification**, because it is the difference between a message that
arrives late and one that never arrives: 429 and 5xx are asked again, a 4xx
is not.

Not the copy, not the HTML, and not whether the platform's own retry loop
works — those belong to the template unit tests and to
`test_notification_email.py`, which drives the real service.
"""

import json

import httpx
import pytest

from app.platform.email import (
    EmailMessage,
    PermanentEmailFailure,
    ResendEmailProvider,
    ResendUnavailable,
)
from app.platform.email.resend import RESEND_SEND_URL

MESSAGE = EmailMessage(
    to="player@example.com",
    subject="Arena64 — Tournament registration confirmed",
    text_body="You are registered for Sunday Open.",
    html_body="<p>You are registered for Sunday Open.</p>",
    idempotency_key="019fe400-0000-7000-8000-000000000001",
)


def _provider(handler: httpx.MockTransport) -> ResendEmailProvider:
    """The production class, with only its socket replaced."""
    return ResendEmailProvider(
        api_key="re_test_key",
        from_address="no-reply@arena64.gg",
        from_name="Arena64",
        client=httpx.AsyncClient(transport=handler),
    )


class TestTheRequest:
    async def test_it_sends_what_resend_needs_and_nothing_else(self) -> None:
        """§10.3. The whole contract, in one assertion each.

        The sender is the one this platform is entitled to use — an address
        outside the verified `arena64.gg` domain is rejected by Resend and
        recorded here as a permanent failure, so getting it wrong is not a
        cosmetic bug.
        """
        seen: dict[str, object] = {}

        def handle(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["auth"] = request.headers.get("authorization")
            seen["idempotency"] = request.headers.get("idempotency-key")
            seen["body"] = json.loads(request.read())
            return httpx.Response(200, json={"id": "resend-message-1"})

        message_id = await _provider(httpx.MockTransport(handle)).send(MESSAGE)

        assert seen["url"] == RESEND_SEND_URL
        assert seen["auth"] == "Bearer re_test_key"
        # Deterministic across retries by construction — the notification's
        # own id — which is what makes a request that timed out *after*
        # acceptance safe to repeat.
        assert seen["idempotency"] == MESSAGE.idempotency_key

        # Asserted on the parsed body rather than the serialised string:
        # §9 forbids coupling to whitespace, and a client that changed its
        # JSON separators would otherwise fail a test about a sender address.
        assert seen["body"] == {
            "from": "Arena64 <no-reply@arena64.gg>",
            "to": ["player@example.com"],
            "subject": MESSAGE.subject,
            "text": MESSAGE.text_body,
            # Both parts, always — §17. An HTML-only transactional email is
            # what this key's presence prevents.
            "html": MESSAGE.html_body,
        }

        assert message_id == "resend-message-1"

    async def test_a_message_with_no_key_carries_no_idempotency_header(self) -> None:
        """`auth`'s two messages have nothing stable to key on.

        A resend of a verification link is a **new** message the player
        asked for, and keying it would make the second request silently
        return the first one's result — the opposite of what they wanted.
        """
        seen: dict[str, object] = {}

        def handle(request: httpx.Request) -> httpx.Response:
            seen["idempotency"] = request.headers.get("idempotency-key")
            return httpx.Response(200, json={"id": "resend-message-2"})

        unkeyed = EmailMessage(
            to="player@example.com",
            subject="Arena64 — Verify your email",
            text_body="Follow this link.",
        )
        await _provider(httpx.MockTransport(handle)).send(unkeyed)

        assert seen["idempotency"] is None


class TestClassification:
    @pytest.mark.parametrize("status", [408, 409, 429, 500, 502, 503])
    async def test_a_transient_answer_is_retryable(self, status: int) -> None:
        """§10.4. Rate limiting and every server-side fault.

        `408` and `409` are the two 4xx that belong here: a request timeout
        never arrived, and a conflict on an idempotency key means a
        concurrent attempt is in flight. Both are answered by asking again.
        """
        provider = _provider(
            httpx.MockTransport(lambda _: httpx.Response(status, json={"message": "nope"}))
        )

        with pytest.raises(ResendUnavailable):
            await provider.send(MESSAGE)

    @pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
    async def test_a_refusal_is_permanent(self, status: int) -> None:
        """§10.5. A malformed address, a rejected sender, a bad credential.

        Retrying is asking the same question and being told no again, and the
        delivery service reads this exception as terminal — so a bounced
        address stops costing attempts the same day.
        """
        provider = _provider(
            httpx.MockTransport(lambda _: httpx.Response(status, json={"message": "no"}))
        )

        with pytest.raises(PermanentEmailFailure):
            await provider.send(MESSAGE)

    async def test_a_transport_fault_propagates_as_retryable(self) -> None:
        """A timeout, a refused connection, a DNS failure.

        Left to propagate rather than wrapped: the delivery service treats
        anything that is not `PermanentEmailFailure` as retryable, and
        catching this only to re-raise a different type would be a second
        vocabulary for the same answer.
        """

        def handle(_: httpx.Request) -> httpx.Response:
            raise httpx.ConnectTimeout("resend did not answer")

        with pytest.raises(httpx.HTTPError):
            await _provider(httpx.MockTransport(handle)).send(MESSAGE)


class TestTheMessageId:
    @pytest.mark.parametrize(
        "response",
        [
            httpx.Response(200, json={}),
            httpx.Response(200, json=["not", "a", "dict"]),
            httpx.Response(200, json={"id": 7}),
            httpx.Response(200, content=b"not json"),
        ],
    )
    async def test_an_unusable_body_costs_the_id_and_not_the_send(
        self, response: httpx.Response
    ) -> None:
        """§10.6. The message was accepted; the reference is a convenience.

        A provider that changed its response shape must cost a missing id
        rather than a failed delivery for a message that already went — a
        retry would then send a second copy of something that arrived.
        """
        provider = _provider(httpx.MockTransport(lambda _: response))

        assert await provider.send(MESSAGE) is None
