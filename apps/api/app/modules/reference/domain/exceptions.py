"""`reference`'s one failure — A64-020.5A-pre §22.

A `ValidationError` rather than a `NotFoundError`, and the distinction is
about whose mistake it is: nobody asks for `/reference/time-controls/{id}`
as a resource. The identifier arrives as a **field on a queue join**, and a
field naming something the platform does not offer is a malformed request —
the same category `InvalidUsername` is in, and the same `422` a body with an
unknown enum member already gets from FastAPI.
"""

from typing import ClassVar

from app.core.error_codes import ErrorCode
from app.core.exceptions import ValidationError


class UnsupportedTimeControl(ValidationError):
    """The requested time control is not one this platform currently offers.

    **One exception for two causes**, deliberately: an identifier no
    catalogue row matches, and a row whose `is_active` is `false`. A client
    cannot act differently on them — both mean "the menu you are holding is
    out of date, read it again" — and publishing the difference would
    announce which controls exist but are withdrawn, which is a product
    decision rather than an API fact.

    The *operator* still learns which of the two happened, because the
    catalogue reader logs it. That is the same split
    `MatchAcceptanceService` applies to a match that does not exist versus
    one the caller is not in.
    """

    default_code: ClassVar[ErrorCode] = ErrorCode.UNSUPPORTED_TIME_CONTROL


__all__ = ["UnsupportedTimeControl"]
