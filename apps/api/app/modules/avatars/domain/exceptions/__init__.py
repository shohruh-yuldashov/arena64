"""This module's typed failures, on the platform hierarchy in
`app.core.exceptions` — never a parallel one.

Inheriting from the existing tree is what makes these work end to end with
no per-module wiring: `app/api/exception_handlers.py` maps by walking an
exception's MRO, so `UnsupportedImageFormat(ValidationError)` already
returns `422` without `avatars` registering a handler.

Every rejection below is a `ValidationError` (422) rather than a
`RuleViolationError` or a bespoke 400, because each one describes an input
the caller can fix by sending different bytes. The client's action is
identical in every case — pick another file — which is also why none of
them carries a wire code of its own beyond `AvatarTooLarge`; see that
class.
"""

from typing import ClassVar

from app.core.error_codes import ErrorCode
from app.core.exceptions import NotFoundError, ValidationError


class InvalidAvatarImage(ValidationError):
    """The uploaded bytes are not a usable image.

    The catch-all, and the parent of the two specific reasons below so a
    caller can catch one type and be sure it covers all of them.

    Raised when the signature matched an accepted format but the decoder
    could not read the file — a truncated PNG, a JPEG whose body is
    garbage, a WebP with a valid RIFF header and nothing after it. In other
    words: the two-stage check doing its job, where stage one passed and
    stage two did not.

    **The message never quotes the uploaded bytes or the filename.** An
    error string is the most common way user-supplied content reaches a log
    or a screenshot (services.md §8.5), and here the content is a file
    somebody chose off their own disk.
    """


class UnsupportedImageFormat(InvalidAvatarImage):
    """The file's signature is not one of the accepted formats — 422.

    Raised by the *signature* check, before any decoding. That ordering is
    the point: a `.exe` renamed to `.png` is rejected by three bytes of
    comparison rather than by an image decoder, which is both cheaper and
    narrower than asking a parser to fail safely.

    Carries no wire code of its own. The client's action — choose a JPEG,
    PNG or WebP — is the same as for any other malformed upload, and the
    message names the accepted formats.
    """


class EmptyAvatarUpload(InvalidAvatarImage):
    """Nothing was sent — 422.

    Its own type rather than an empty-bytes special case of the above,
    because it is almost always a *client* bug rather than a bad file: a
    form submitted with no file selected, or a multipart body assembled
    without a payload. Distinguishing it in the logs is what makes that
    diagnosable.
    """


class AvatarTooLarge(ValidationError):
    """The upload exceeds the maximum size — 413 in spirit, 422 here.

    The one avatar rejection with its own wire code, because it is the one
    a client can act on *before* retrying: a UI can compress or re-encode
    and try again, where every other failure means "choose a different
    file". That is the test `app.core.error_codes.ErrorCode` sets for
    earning a code.

    Note the status. RFC 9110 §15.5.14 defines `413 Content Too Large`, and
    that is arguably the more precise answer — but on this platform the
    limit is discovered by *counting bytes as they arrive*, so a rejection
    is a validation outcome reached mid-body rather than a refusal of a
    declared length. Keeping it a `422` also keeps every avatar failure on
    one status with one body shape, which is what lets a client branch on
    `code` alone. Recorded because the alternative is defensible and
    somebody will ask.
    """

    default_code: ClassVar[ErrorCode] = ErrorCode.AVATAR_TOO_LARGE


class AvatarNotFound(NotFoundError):
    """The account has no avatar — 404.

    Raised by `GET /profile/avatar` only. **Not** by `DELETE`, which is
    idempotent and succeeds for a player who has none: a caller retrying
    after a dropped response must not get an error for the retry
    (CLAUDE.md §3 rule 8).
    """


__all__ = [
    "AvatarNotFound",
    "AvatarTooLarge",
    "EmptyAvatarUpload",
    "InvalidAvatarImage",
    "UnsupportedImageFormat",
]
