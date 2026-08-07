"""`WebPushProvider` — Arena64's push transport — A64-021.6 §16, §17.

One adapter behind the provider-neutral port, speaking the Web Push
protocol (RFC 8030) to whatever push service a browser's endpoint names. It
is deliberately vendor-blind: Mozilla, Google and Apple each run their own
service, and nothing in this class parses an endpoint or branches on its
host. A URL that arrived from a browser is a URL.

Encryption is RFC 8291 (`encryption.py`) and identity is RFC 8292
(`vapid.py`). What is left here is one POST, its headers, and the reading of
the answer.

## Why a narrow adapter rather than `pywebpush`

Recorded in `pyproject.toml`, and it is the argument A64-021.5 made for
Resend, unchanged: `pywebpush.webpush()` is synchronous, and this platform's
only caller is the delivery worker, whose entire job is I/O. Using it means
either blocking the event loop for a network round trip per subscription or
a thread per message, and it offers no per-request timeout.

## What never leaves this file

An endpoint, a subscription key, a payload, and a push service's response
body. The classification that escapes is an exception type; the log lines
carry a status code and nothing else. A push service's error text can quote
the endpoint it rejected, which is why none of it is logged or re-raised.
"""

import logging
from typing import Final

import httpx

from app.platform.push.encryption import encrypt
from app.platform.push.message import (
    PermanentPushFailure,
    PushMessage,
    TransientPushFailure,
)
from app.platform.push.vapid import VapidSigner

logger = logging.getLogger(__name__)

#: The default request budget.
#:
#: Ten seconds, matching the email transport, and for the same reason: what
#: this bounds is a push service that has stopped answering, not one that is
#: slow. Nobody is waiting on a push — it is not interactive — so a tighter
#: budget buys latency no user perceives while letting a healthy-but-loaded
#: service fail a batch.
DEFAULT_TIMEOUT_SECONDS: Final = 10.0

#: The subscription is finished — RFC 8030 §7.3.
#:
#: `404` means the push service never had it; `410` means it did and the
#: browser has gone. Both are terminal for this subscription and neither
#: says anything about the platform: this is the ordinary end of a browser
#: profile that was cleared, a PWA that was uninstalled, or a permission
#: that was revoked.
_GONE_STATUSES: Final[frozenset[int]] = frozenset({404, 410})

#: Answered, and the answer was *try again*.
#:
#: `429` is the push service rate-limiting this sender, and every `5xx` is
#: its own fault. `408` is a request that never arrived. Everything else in
#: the 4xx range is this platform's request being wrong, and asking again is
#: asking the same question.
_RETRYABLE_STATUSES: Final[frozenset[int]] = frozenset({408, 429})


class WebPushProvider:
    """Sends one encrypted message per subscription. `platform.push.PushProvider`.

    Holds a client and a signer, and is built once per process: an
    `httpx.AsyncClient` owns a connection pool, and one per message would
    open a TLS connection per notification — which for a tournament round is
    one per player per device.
    """

    def __init__(
        self,
        *,
        signer: VapidSigner,
        client: httpx.AsyncClient | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._signer = signer
        self._timeout = timeout_seconds
        # Injected for the contract suite, which points it at a stub server
        # rather than at a real push service — §28 forbids an automated test
        # contacting one, and a client is the narrowest thing to substitute.
        self._client = client or httpx.AsyncClient(timeout=timeout_seconds)

    async def send(self, message: PushMessage) -> None:
        """Encrypts and delivers one message.

        The split between the two failure types lives here rather than in a
        service, because only this class can read a status code and a
        service branching on one would be a service that knows a protocol.
        """
        recipient = message.recipient
        try:
            body = encrypt(
                plaintext=message.payload,
                ua_public_key=recipient.p256dh,
                auth_secret=recipient.auth,
            )
            authorization = self._signer.authorization_for(recipient.endpoint)
        except ValueError as malformed:
            # A key that does not parse, an endpoint that is not https, or a
            # payload too large. None of these improve with a retry: the
            # first two mean the stored subscription is unusable and the
            # third is a defect in the caller's payload.
            #
            # The message is this platform's own — `ValueError` here is
            # raised by code in this package — so it carries no push service
            # text and is safe to chain.
            logger.warning("web_push_unsendable", extra={"reason": str(malformed)})
            raise PermanentPushFailure(str(malformed)) from malformed

        headers = {
            "Authorization": authorization,
            "Content-Encoding": "aes128gcm",
            "Content-Type": "application/octet-stream",
            "TTL": str(message.ttl_seconds),
            "Urgency": message.urgency,
        }

        try:
            response = await self._client.post(
                recipient.endpoint,
                content=body,
                headers=headers,
                timeout=self._timeout,
            )
        except httpx.HTTPError as transport:
            # A timeout, a refused connection, a DNS failure, a TLS error.
            # All retryable, and none logged with `exc_info` — an `httpx`
            # exception's message carries the URL, which is the endpoint.
            logger.warning("web_push_request_failed", extra={"reason": "transport"})
            raise TransientPushFailure("push service did not answer") from transport

        if response.status_code in _GONE_STATUSES:
            # Not a warning. This is the expected end of every subscription's
            # life and logging it as a problem would fill an operator's error
            # budget with browsers that were closed.
            logger.info("web_push_subscription_gone", extra={"status": response.status_code})
            raise PermanentPushFailure(f"subscription is gone ({response.status_code})")

        if response.status_code in _RETRYABLE_STATUSES or response.status_code >= 500:
            logger.warning("web_push_unavailable", extra={"status": response.status_code})
            raise TransientPushFailure(f"push service returned {response.status_code}")

        if response.status_code >= 400:
            # A 4xx this platform caused — a malformed header, a rejected
            # assertion, a body the service will not take. Terminal, and the
            # *reason* stays inside this call: the error body quotes the
            # endpoint it rejected.
            logger.warning("web_push_refused", extra={"status": response.status_code})
            raise PermanentPushFailure(f"push service refused the request ({response.status_code})")


__all__ = ["DEFAULT_TIMEOUT_SECONDS", "WebPushProvider"]
