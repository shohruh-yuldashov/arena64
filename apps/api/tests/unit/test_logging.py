"""The log formatters — A64-021.2H.

Two tests, and both are about the same defect: every structured field this
platform logs was being computed and thrown away.

`app/common/logging.py` built each line from a **fixed** list of record
attributes, so `extra={"event_id": ..., "outcome": ...}` — which this
codebase passes at essentially every log call — reached the handler and was
discarded. `event_queued` logged no event id, `notification_pushed` no
outcome, `outbox_tick_completed` no counts. CLAUDE.md §8 rule 1 asks for
key–value or JSON fields "never interpolated prose that must be regex-parsed
later", and prose with the fields removed is what came out.

It was found the hard way: diagnosing a notification that never arrived
meant reading a log in which every line on the path existed and none of them
said anything.
"""

import json
import logging

import pytest

from app.common.logging import configure_logging
from app.config.environment import Environment

MESSAGE = "outbox_tick_completed"
FIELDS = {
    "claimed": 3,
    "published": 3,
    "skipped": 1,
    "skipped_event_types": ["friends.friend_request_sent"],
    "worker_id": "worker-a",
}


def _emit(fmt: str, capsys: pytest.CaptureFixture[str], **extra: object) -> str:
    """One line, through the **real** configuration.

    `configure_logging` is what a process calls at start, and it is what
    decides which formatter is installed — so a test that built a formatter
    directly would be asserting against an object no deployment uses.
    """
    configure_logging(level="INFO", environment=Environment.LOCAL, format_override=fmt)
    logging.getLogger("tests.logging").info(MESSAGE, extra=dict(extra))
    return capsys.readouterr().out.strip()


class TestStructuredFields:
    def test_json_output_carries_the_caller_s_fields(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The production format, and the one an aggregator queries.

        Asserted as a parsed object rather than by substring: "the field is
        somewhere in the line" would also pass for a formatter that dumped
        `record.__dict__` into the message, which is the shape §8 rule 1
        rules out.
        """
        line = _emit("json", capsys, **FIELDS)

        payload = json.loads(line)
        assert payload["message"] == MESSAGE
        for key, value in FIELDS.items():
            assert payload[key] == value

        # The envelope is still the envelope: a caller cannot overwrite the
        # keys an aggregator filters and routes on.
        assert payload["level"] == "INFO"
        assert payload["logger"] == "tests.logging"

    def test_a_caller_cannot_rewrite_the_envelope(self, capsys: pytest.CaptureFixture[str]) -> None:
        """`extra` is detail, never metadata.

        A field named `level` or `logger` must not decide how a line is
        filtered — an aggregator's severity routing would then be settable
        by whoever wrote the call site.
        """
        line = _emit("json", capsys, logger="not-this", detail="kept")

        payload = json.loads(line)
        assert payload["logger"] == "tests.logging"
        assert payload["detail"] == "kept"

    def test_human_output_appends_the_fields_after_the_message(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The development format, where this was discovered.

        After the message rather than before it, so the thing a human scans
        for stays at a predictable column.
        """
        line = _emit("human", capsys, **FIELDS)

        assert MESSAGE in line
        assert line.index(MESSAGE) < line.index("claimed=3")
        assert "skipped=1" in line
        assert "worker_id=worker-a" in line

    def test_a_line_with_no_fields_is_unchanged(self, capsys: pytest.CaptureFixture[str]) -> None:
        """No trailing space, no empty braces. Most third-party lines carry
        no `extra`, and they must not become noisier for this."""
        line = _emit("human", capsys)

        assert line.endswith(MESSAGE)
