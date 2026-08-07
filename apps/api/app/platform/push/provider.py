"""Choosing the push transport — A64-021.6 §6, and the availability it decides.

One function, and it is the **only** place on this platform that decides
whether a process can send a push notification at all.

## The key pair is the switch

There is no `PUSH_ENABLED` flag, and adding one would be the settings-screen
lie §6 forbids: a boolean saying "push works" can disagree with whether a
key pair exists, and the player is the one who finds out — by turning on a
switch that turns on nothing.

So the question is asked of the configuration itself. A process holding a
valid VAPID key pair can send; one that does not, cannot, and
`ChannelAvailability` is built from exactly this answer.

## Why a missing key pair is not a boot failure

Unlike `RESEND_API_KEY`, whose absence makes registration unverifiable and
therefore stops the platform working, an absent VAPID pair costs one
optional channel. A deployment that has not generated keys yet reports push
unavailable, refuses to store subscriptions, and shows a settings screen
that says so — all of which are true.

A *malformed* pair is different and does raise: it means somebody intended
to configure push and got it wrong, and the failure mode of accepting it is
that every subscription created afterwards is bound to a key that cannot
sign for it. That damage is not undone by fixing the configuration later —
see `vapid.py` on why the key pair is operational state.
"""

import logging

from app.config.settings import PushSettings
from app.platform.push.message import PushProvider
from app.platform.push.vapid import VapidKeyPair, VapidSigner
from app.platform.push.webpush import WebPushProvider

logger = logging.getLogger(__name__)


def build_vapid_keys(settings: PushSettings) -> VapidKeyPair | None:
    """The key pair this process holds, or `None` when push is unconfigured.

    Raises `ValueError` for a pair that is present and wrong — see the
    module docstring on why that asymmetry is deliberate.
    """
    private = settings.vapid_private_key
    public = settings.vapid_public_key
    if private is None or public is None:
        return None

    return VapidKeyPair.from_base64(
        private_key=private.get_secret_value(),
        public_key=public,
        subject=settings.vapid_subject,
    )


def build_push_provider(keys: VapidKeyPair | None) -> PushProvider | None:
    """The transport this process sends through, or `None`.

    Takes the parsed key pair rather than the settings, so the caller parses
    once and this cannot answer differently from `build_vapid_keys` — two
    functions reading the same configuration are two chances to disagree
    about whether push works.

    Built **once per process**: `WebPushProvider` owns an HTTP connection
    pool, and one per message would open a TLS connection per device.
    """
    if keys is None:
        logger.info("push_transport_absent")
        return None

    # The subject, and nothing else. It is a contact address a push service
    # operator may use, is not a secret, and is the one field that tells an
    # operator reading a boot log *which* configuration was loaded.
    logger.info("push_transport_ready", extra={"subject": keys.subject})
    return WebPushProvider(signer=VapidSigner(keys))


def can_deliver_push(settings: PushSettings) -> bool:
    """Whether this process, as configured, can send a push notification.

    The same question `build_vapid_keys` answers, asked without building
    anything — for the availability matrix, which runs on a settings read
    rather than at the composition root.
    """
    return settings.vapid_private_key is not None and settings.vapid_public_key is not None


__all__ = ["build_push_provider", "build_vapid_keys", "can_deliver_push"]
