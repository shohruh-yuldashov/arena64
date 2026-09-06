"""What notification retention publishes — A64-028.7, closing P2-7.

`RetentionRelation` has one member per relation the run prunes, and the
service's result has one field per member. A relation that is pruned and
**not counted** is one whose growth is invisible until it is the incident,
which is the failure retention exists to prevent — so the correspondence is
asserted by a test rather than left as a convention.
"""

from enum import StrEnum
from typing import Final

#: Rows removed, by relation. Zero is recorded rather than skipped.
RETENTION_DELETIONS: Final = "notifications.retention_deletions_total"


class RetentionRelation(StrEnum):
    NOTIFICATION = "notification"
    EMAIL_DELIVERY = "email_delivery"
    PUSH_DELIVERY = "push_delivery"
    REVOKED_SUBSCRIPTION = "revoked_subscription"


__all__ = ["RETENTION_DELETIONS", "RetentionRelation"]
