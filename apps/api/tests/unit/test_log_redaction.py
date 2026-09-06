"""Secrets do not reach a log line — A64-028.6 §18, closing P2-2.

A64-028.1 found that `_JsonFormatter` emitted whatever `extra={…}` a call
site passed, with no redaction anywhere, and that the only protection was
call-site discipline. The discipline was good. This is what makes the one
call site that forgets harmless instead of catastrophic.

Every test here asserts on the **captured output**, not on the filter's
return value: what matters is what would have been shipped to whatever
collects stdout.
"""

import io
import json
import logging

import pytest

from app.common.logging import configure_logging
from app.common.redaction import REDACTED, is_sensitive, redact
from app.config.environment import Environment


@pytest.fixture
def captured() -> io.StringIO:
    configure_logging(level="INFO", environment=Environment.PRODUCTION)
    buffer = io.StringIO()
    logging.getLogger().handlers[0].stream = buffer  # type: ignore[attr-defined]
    return buffer


def _emit(captured: io.StringIO, **fields: object) -> dict[str, object]:
    logging.getLogger("test.redaction").info("probe", extra=fields)
    payload: dict[str, object] = json.loads(captured.getvalue().strip())
    return payload


class TestCredentialsNeverReachTheOutput:
    @pytest.mark.parametrize(
        "field",
        [
            "authorization",
            "cookie",
            "set_cookie",
            "refresh_token",
            "access_token",
            "token",
            "password",
            "reset_token",
            "otp",
            "otp_secret",
            "jwt_secret_key",
            "api_key",
            "vapid_private_key",
            "postgres_dsn",
        ],
    )
    def test_the_value_is_replaced(self, captured: io.StringIO, field: str) -> None:
        payload = _emit(captured, **{field: "s3cr3t-value"})

        assert payload[field] == REDACTED
        assert "s3cr3t-value" not in captured.getvalue()

    def test_the_key_survives_so_an_operator_knows_it_was_there(
        self, captured: io.StringIO
    ) -> None:
        """Dropping the field would be indistinguishable from a call site
        that never passed one, and those are different facts."""
        assert "refresh_token" in _emit(captured, refresh_token="x")


class TestPersonalDataIsTreatedAsSensitive:
    def test_an_address_is_redacted(self, captured: io.StringIO) -> None:
        payload = _emit(captured, email="player@example.test")

        assert payload["email"] == REDACTED
        assert "player@example.test" not in captured.getvalue()


class TestWhatMustSurvive:
    """A log an operator cannot reconstruct an incident from is not a safer
    log, it is a useless one — see `common/redaction.py` on where the line
    is drawn, and `platform/metrics/__init__.py` on why it is drawn there.
    """

    @pytest.mark.parametrize(
        "field", ["user_id", "match_id", "tournament_id", "consumer", "event_type"]
    )
    def test_identifiers_pass_through(self, captured: io.StringIO, field: str) -> None:
        assert _emit(captured, **{field: "keep-me"})[field] == "keep-me"

    def test_the_correlation_identifiers_are_present(self, captured: io.StringIO) -> None:
        """Not in the list above because `_ContextFilter` owns them — it
        overwrites whatever a call site passed with the contextvar, which is
        the point of it. What matters here is only that redaction did not
        remove the keys."""
        payload = _emit(captured)

        assert {"request_id", "correlation_id", "causation_id"} <= set(payload)

    def test_the_token_family_is_an_identifier_and_not_a_credential(
        self, captured: io.StringIO
    ) -> None:
        """The field A64-028.2's reuse detection is reconstructed from. A
        substring rule would redact it; the allow-list is why it does not."""
        assert _emit(captured, token_family="tf-1")["token_family"] == "tf-1"


class TestTheHumanFormatterIsCoveredToo:
    def test_a_local_run_redacts_as_well(self) -> None:
        """A redaction that only ran for JSON would leave every developer's
        machine unprotected — which is where a token is most likely to be
        printed by hand while debugging."""
        configure_logging(level="INFO", environment=Environment.LOCAL)
        buffer = io.StringIO()
        logging.getLogger().handlers[0].stream = buffer  # type: ignore[attr-defined]

        logging.getLogger("test.redaction").info("probe", extra={"password": "hunter2"})

        assert "hunter2" not in buffer.getvalue()
        assert REDACTED in buffer.getvalue()


class TestTheRuleItself:
    def test_matching_is_case_insensitive(self) -> None:
        assert is_sensitive("Authorization")
        assert is_sensitive("REFRESH_TOKEN")

    def test_an_allowed_name_wins_over_the_substring(self) -> None:
        assert not is_sensitive("token_family")

    def test_redact_applies_the_same_rule_to_a_dictionary(self) -> None:
        """For the callers that log a structure rather than keyword fields,
        where the boundary filter sees one opaque value."""
        assert redact({"password": "p", "user_id": "u"}) == {"password": REDACTED, "user_id": "u"}
