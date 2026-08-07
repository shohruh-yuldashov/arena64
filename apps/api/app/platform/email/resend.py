"""`ResendEmailProvider` — Arena64's production transport.

One adapter, behind the provider-neutral port, sending through Resend's
`POST /emails`. Every message this platform sends comes through here: the
verification link, the password reset, and the notification email. §2 is
explicit that there is one transport and not one per caller, and the reason
is the one duplication always has — two would be two sender identities, two
retry stories, and two places a credential is configured.

## Why a narrow HTTP adapter rather than the `resend` SDK

CLAUDE.md §2.6: *do not add a dependency for what an existing one does*, and
audit what a new one costs. Weighed against the SDK:

    the surface is one endpoint   `POST https://api.resend.com/emails`, a
                                  JSON body of five fields and a bearer
                                  header. The SDK's value is proportional to
                                  the API it wraps, and this is not much API
    async, without a thread       the SDK's send is synchronous. Calling it
                                  from this worker means either blocking the
                                  event loop for a network round trip or
                                  wrapping every send in a thread — and the
                                  worker is the one component whose whole job
                                  is I/O
    the timeout is the point      §2 requires a bounded one. `httpx` takes it
                                  per request and this class refuses to be
                                  constructed without one; an SDK's default
                                  is whatever its transport chose
    no vendor types escape        `httpx` is already this repository's HTTP
                                  client. Nothing about Resend appears in a
                                  signature, a return type or an exception
                                  outside this file

The cost is that a future Resend feature means writing a few lines here
rather than upgrading a package. That is the correct trade for an endpoint
this small.

## What never leaves this file

A recipient address, a subject, a body, an API key, and a response body.
§3 and §11: the classification that escapes is an exception type, the value
that escapes is a message id, and the log lines carry a status code and
nothing else. A vendor's error text can quote the address it rejected, which
is why none of it is logged or persisted.
"""

import logging
from typing import Any, Final

import httpx

from app.platform.email.message import EmailMessage, PermanentEmailFailure

logger = logging.getLogger(__name__)

#: Resend's send endpoint. One, and the whole API this platform uses.
RESEND_SEND_URL: Final = "https://api.resend.com/emails"

#: The default request budget.
#:
#: Ten seconds is generous for a JSON POST and deliberately so: what this
#: bounds is a provider that has stopped answering, not one that is slow.
#: The worker sends serially, so a tighter budget buys latency nobody is
#: waiting on — email is not interactive — while a looser one lets one
#: unresponsive request hold a batch.
#:
#: A timeout is a **retryable** outcome: the request may well have been
#: accepted, which is exactly why the idempotency key below matters.
DEFAULT_TIMEOUT_SECONDS: Final = 10.0

#: Status codes that mean *ask again later*.
#:
#: `429` and every `5xx`. Everything else in the 4xx range is the provider
#: telling this platform the request itself is wrong — a malformed address, a
#: sender it will not accept, a body it cannot parse — and asking again is
#: asking the same question.
#:
#: `408` and `409` are the two 4xx exceptions and both are genuinely
#: transient: a request timeout is a request that never arrived, and a
#: conflict on an idempotency key means a concurrent attempt is in flight.
_RETRYABLE_STATUSES: Final[frozenset[int]] = frozenset({408, 409, 429})


class ResendEmailProvider:
    """Sends through Resend. `platform.email.EmailProvider`.

    Holds a client, a credential and a sender identity, and is built once per
    process: an `httpx.AsyncClient` owns a connection pool, and constructing
    one per message would open a TLS connection per email.
    """

    def __init__(
        self,
        *,
        api_key: str,
        from_address: str,
        from_name: str,
        client: httpx.AsyncClient | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._api_key = api_key
        # Composed once. Resend accepts RFC 5322's display-name form, and
        # composing it per send would be three string operations per message
        # for a value that cannot change while the process runs.
        self._sender = f"{from_name} <{from_address}>"
        self._timeout = timeout_seconds
        # Injected for the contract suite, which points it at a stub server
        # rather than at Resend — §8 forbids an automated test contacting a
        # provider, and a client is the narrowest thing to substitute to make
        # that true without special-casing this class.
        self._client = client or httpx.AsyncClient(timeout=timeout_seconds)

    async def send(self, message: EmailMessage) -> str | None:
        """Hands one message to Resend. Returns their id for it.

        Raises `PermanentEmailFailure` for a rejection that will recur, and
        lets everything else propagate — which `EmailDeliveryService` reads
        as retryable. That split is here rather than in a service because
        only this class can read a status code, and a service branching on
        one would be a service that knows a vendor.
        """
        payload: dict[str, Any] = {
            "from": self._sender,
            "to": [message.to],
            "subject": message.subject,
            "text": message.text_body,
        }
        if message.html_body is not None:
            payload["html"] = message.html_body

        headers = {"Authorization": f"Bearer {self._api_key}"}
        if message.idempotency_key is not None:
            # Resend deduplicates on this for 24 hours. It covers the one
            # case this platform's own delivery table cannot: a request that
            # timed out *after* the provider accepted it. The row is retried
            # — correctly, since nothing knows it arrived — and the provider
            # recognises the repeat instead of sending a second copy.
            headers["Idempotency-Key"] = message.idempotency_key

        try:
            response = await self._client.post(
                RESEND_SEND_URL,
                json=payload,
                headers=headers,
                timeout=self._timeout,
            )
        except httpx.HTTPError:
            # Every transport-level fault: a timeout, a refused connection, a
            # DNS failure, a TLS error. All retryable, and none logged with
            # `exc_info` — an `httpx` exception's message carries the URL,
            # and a future one could carry more.
            logger.warning("resend_request_failed", extra={"reason": "transport"})
            raise

        if response.status_code in _RETRYABLE_STATUSES or response.status_code >= 500:
            logger.warning("resend_rejected", extra={"status": response.status_code})
            raise ResendUnavailable(f"resend returned {response.status_code}")

        if response.status_code >= 400:
            # A 4xx this platform caused. Retrying is asking the same
            # question, so it is terminal — and the *reason* stays inside
            # this call: a Resend error body quotes the address it rejected.
            logger.warning("resend_refused", extra={"status": response.status_code})
            raise PermanentEmailFailure(f"resend refused the request ({response.status_code})")

        return _message_id(response)


class ResendUnavailable(Exception):
    """Resend answered, and the answer was *try again*.

    Its own type rather than a bare `Exception` so an operator reading a
    traceback sees which side failed, and so the retryable path is a named
    thing rather than the absence of `PermanentEmailFailure`.

    It carries a status code and never a body — see this module's docstring.
    """


def _message_id(response: httpx.Response) -> str | None:
    """Resend's id for the message, or `None`.

    Defensive on purpose, and not out of habit: this value is written to a
    database column, and a provider that changed its response shape should
    cost a missing id rather than a failed delivery for a message that was
    accepted. The send succeeded; the reference is a convenience for an
    operator.
    """
    try:
        body = response.json()
    except ValueError:
        return None
    if not isinstance(body, dict):
        return None
    identifier = body.get("id")
    return identifier if isinstance(identifier, str) else None


__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "RESEND_SEND_URL",
    "ResendEmailProvider",
    "ResendUnavailable",
]
