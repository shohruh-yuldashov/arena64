"""Web Push configuration — A64-028.2 §20, §21.

A64-028.1 P2-4: `PushSettings` documented "absent is allowed; wrong is
not", and a *half* pair was neither. `build_vapid_keys` returns `None` the
moment either key is missing, so a tier that set the public key and forgot
its private half booted cleanly, reported push unavailable and refused every
subscription — with nothing saying why, and with the settings screen
truthfully showing the channel off so nobody looked at the configuration.
"""

import pytest
from pydantic import SecretStr
from pydantic import ValidationError as PydanticValidationError

from app.config.settings import PushSettings
from app.platform.push.provider import build_vapid_keys, can_deliver_push

# Shape-valid rather than real: the pair check runs before anything parses
# base64, and these must never be usable keys in a repository.
PUBLIC = "BEl62iUYgUivxIkv69yViEuiBIa-Ib9-SkTBQOsSBDGRQ8kFqIt5cQfqTLPHtDX3sT0"
PRIVATE = "AbCdEfGhIjKlMnOpQrStUvWxYz0123456789-_aBcDeFgH"


def push(**environment: object) -> PushSettings:
    """Built from the variable names, because that is the only way in.

    The fields carry `validation_alias`, so `VAPID_PUBLIC_KEY` is the
    parameter and `vapid_public_key` is not — which is the point of the
    alias and worth exercising. `model_validate` honours it; the constructor
    keyword does not exist for either type checker to accept.
    """
    return PushSettings.model_validate(environment)


class TestThePairIsWholeOrAbsent:
    def test_neither_key_leaves_push_off(self) -> None:
        # The documented, supported state: push is optional and a tier that
        # never generated keys says so.
        settings = PushSettings()

        assert can_deliver_push(settings) is False
        assert build_vapid_keys(settings) is None

    def test_a_public_key_without_its_private_half_refuses_to_start(self) -> None:
        with pytest.raises(PydanticValidationError, match="VAPID_PRIVATE_KEY"):
            push(VAPID_PUBLIC_KEY=PUBLIC)

    def test_a_private_key_without_its_public_half_refuses_to_start(self) -> None:
        with pytest.raises(PydanticValidationError, match="VAPID_PUBLIC_KEY"):
            push(VAPID_PRIVATE_KEY=PRIVATE)

    def test_the_complaint_names_the_variables_and_never_the_values(self) -> None:
        # §21. A configuration error must not be the thing that prints the
        # signing key — into a log, a crash report or an operator's terminal.
        with pytest.raises(PydanticValidationError) as raised:
            push(VAPID_PRIVATE_KEY=PRIVATE)

        rendered = str(raised.value)
        assert "VAPID_PUBLIC_KEY" in rendered
        assert PRIVATE not in rendered

    def test_a_whole_pair_is_accepted_by_the_pair_check(self) -> None:
        # Whether the *contents* parse is `VapidKeyPair.from_base64`'s
        # question, and it still asks it — this check is only about halves.
        settings = push(VAPID_PUBLIC_KEY=PUBLIC, VAPID_PRIVATE_KEY=PRIVATE)

        assert can_deliver_push(settings) is True


class TestTheSigningKeyStaysServerSide:
    def test_the_private_key_does_not_appear_in_a_repr(self) -> None:
        settings = push(VAPID_PUBLIC_KEY=PUBLIC, VAPID_PRIVATE_KEY=PRIVATE)

        assert PRIVATE not in repr(settings)
        assert PRIVATE not in str(settings.vapid_private_key)
        # The public half is meant to be published, and is.
        assert PUBLIC in repr(settings)
