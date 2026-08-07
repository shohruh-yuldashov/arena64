"""The six-digit email verification code — A64-021.5H §2, §3, §5.

Framework-free (architecture.md §8). What lives here is the *policy*: how
long a code lives, how many guesses it survives, how soon another may be
asked for, and what is actually stored in place of it.

## Why a code at all, when a link already worked

A link is a bearer credential in an inbox. It works, and it fails in two
ways that a code does not: a mail client that rewrites URLs breaks it, and a
person reading mail on a phone while registering on a laptop has to move a
session between devices to use it.

A code moves the other way — the person carries six digits from wherever
their mail is to wherever they are signing up. The link stays valid for
already-issued mail (§13) and stops being what a new registration sends.

## Six digits is low entropy, and that is the whole design constraint

A million possibilities is nothing to an offline attacker with the table.
Three things are what make it safe, and all three are load-bearing:

    a keyed verifier   a stolen database row cannot be brute-forced without
                       a secret the database does not hold
    five attempts      an online attacker gets five guesses in ten minutes,
                       so the odds of a hit are 5 in 10^6 per challenge
    ten minutes        the window an attacker has, and the window a person
                       needs to switch to their inbox and back

Removing any one of them makes the other two insufficient. A longer TTL with
five attempts is fine; a longer TTL with *unbounded* attempts is a code that
falls over lunch.
"""

import hmac
import secrets
from hashlib import sha256
from typing import Final
from uuid import UUID

#: How many digits, and therefore how many possibilities.
#:
#: Six, because that is what a person can hold in their head between one
#: application and another. Eight would be meaningfully stronger against
#: online guessing and is the wrong trade: the attempt limit already bounds
#: that, and every extra digit is a person mistyping.
OTP_LENGTH: Final = 6

#: How long a code lives, in minutes.
#:
#: Ten. Long enough to switch to a mail client, wait for delivery and type
#: six digits; short enough that a code left in an unattended inbox is dead
#: before anybody walks past it. A verification *link* lives 24 hours
#: because it is one click from wherever it was received; a code is being
#: carried by hand, and the two windows are not the same question.
OTP_TTL_MINUTES: Final = 10

#: Guesses before the challenge is destroyed.
#:
#: Five. The arithmetic is the point: an attacker who can submit five codes
#: has a 5-in-10^6 chance per challenge, and must request a new one — which
#: is rate-limited — to get five more. Raising it to fifty would make a
#: sustained attack a matter of patience rather than of luck.
#:
#: A person who mistypes five times is asking for a new code, which costs
#: them a minute. That asymmetry is the correct one.
OTP_MAX_ATTEMPTS: Final = 5

#: How long before another code may be asked for, in seconds.
#:
#: Sixty. What it bounds is not brute force — the attempt limit does that —
#: but *mail volume*: a resend button with no cooldown is a way to make this
#: platform send somebody else fifty messages a minute, and a sending domain
#: is a reputation before it is a feature.
OTP_RESEND_COOLDOWN_SECONDS: Final = 60


def generate_otp() -> str:
    """A fresh code, zero-padded to `OTP_LENGTH`.

    `secrets.randbelow`, never `random` and never a timestamp: the module
    docstring's whole argument rests on the code being unguessable, and
    `random` is a Mersenne twister whose state is recoverable from its
    output. `secrets` draws from the OS CSPRNG.

    **Zero-padded, so `000042` is a code this platform can issue.** Drawing
    from `100000..999999` — the obvious way, and the one that avoids
    thinking about leading zeros — discards a tenth of the space and makes
    the first digit non-uniform. Every one of the million values is
    representable, and the frontend accepts six characters rather than a
    number.
    """
    return f"{secrets.randbelow(10**OTP_LENGTH):0{OTP_LENGTH}d}"


def is_well_formed(code: str) -> bool:
    """Whether this is something that could be a code at all.

    Checked before an attempt is counted (§10): a caller who submitted
    `"abc"` or four digits has not guessed anything, and spending one of
    five attempts on a malformed field would let a client's own bug lock a
    person out of their account.

    `str.isdigit` is deliberately not used — it accepts Unicode digits like
    `٤` and superscripts, and a code is ASCII.
    """
    return len(code) == OTP_LENGTH and all(character in "0123456789" for character in code)


def otp_verifier(*, secret: bytes, challenge_id: UUID, user_id: UUID, code: str) -> bytes:
    """What is stored in place of the code — §5.

    **HMAC-SHA256, keyed with a server secret**, and the key is what makes
    it safe. A bare `sha256(code)` over a six-digit space is a table an
    attacker precomputes in under a second; the same attacker holding a
    database dump and no secret cannot invert this at all.

    The message binds the code to *this* challenge and *this* account:

        challenge_id   so a verifier observed on an old row cannot be
                       recognised on a new one, even for the same code
        user_id        so two accounts issued the same code do not share a
                       stored value

    Neither is strictly required — one live challenge per user is enforced
    by a partial unique index, so an old row can never be redeemed — and
    both are one string concatenation. Defence that costs nothing is
    defence worth having.

    Returns 32 bytes, which is what `email_verification_tokens.token_hash`
    already is and already checks. The column did not have to change, and
    that is why an OTP challenge and a link challenge can be one table.
    """
    message = f"{challenge_id}:{user_id}:{code}".encode()
    return hmac.new(secret, message, sha256).digest()


def matches(*, verifier: bytes, expected: bytes) -> bool:
    """Constant-time comparison — §5.

    `hmac.compare_digest`, never `==`. Both values are digests rather than
    secrets, so the timing signal is weaker than it would be on a raw
    comparison, and "weaker" is not a reason to leak it: an attacker who can
    measure how many leading bytes matched can walk a verifier byte by byte
    with far fewer than a million requests.
    """
    return hmac.compare_digest(verifier, expected)


__all__ = [
    "OTP_LENGTH",
    "OTP_MAX_ATTEMPTS",
    "OTP_RESEND_COOLDOWN_SECONDS",
    "OTP_TTL_MINUTES",
    "generate_otp",
    "is_well_formed",
    "matches",
    "otp_verifier",
]
