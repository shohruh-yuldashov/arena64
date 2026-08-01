"""`notifications` — social events become notifications.

The bounded context database.md §229 names, entered from the consumer end.
It owns no relation yet: `notification` and `notification_delivery` belong to
NT-1's persisted history, which A64-013.7 does not build (see
`domain/notification.py` on why building a record of deliveries before there
are delivery channels would be the wrong half).

What it does own is the two halves of the social notification path, which sit
on either side of the outbox:

    application/services/presence_notification_service.py
        the **producer**. Detects presence edges and enqueues events.
        `auth`'s lifecycle routes hold this, never `PresenceService`

    application/services/social_notification_dispatcher.py
        the **consumer**. An `EventHandler` the relay routes to: re-reads
        relationships, re-checks blocking, renders through the privacy gate,
        hands the result to a sink

Nothing here is reachable over HTTP. A64-013.7: "no public notification
APIs. Only internal infrastructure."
"""
