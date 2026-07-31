"""app.core.identifiers.generate_uuid7 — no database needed; this is pure
Python."""

import time
import uuid

from app.core.identifiers import generate_uuid7


def _timestamp_ms(value: object) -> int:
    return int(value.hex[:12], 16)  # type: ignore[attr-defined]


class TestGenerateUuid7:
    def test_is_version_7(self) -> None:
        assert generate_uuid7().version == 7

    def test_is_rfc_9562_variant(self) -> None:
        assert generate_uuid7().variant == uuid.RFC_4122

    def test_two_calls_never_collide(self) -> None:
        assert generate_uuid7() != generate_uuid7()

    def test_a_thousand_calls_are_all_unique(self) -> None:
        ids = {generate_uuid7() for _ in range(1000)}
        assert len(ids) == 1000

    def test_timestamps_are_monotonic_non_decreasing_across_a_time_gap(self) -> None:
        first = generate_uuid7()
        time.sleep(0.005)
        second = generate_uuid7()
        assert _timestamp_ms(second) >= _timestamp_ms(first)

    def test_embedded_timestamp_is_close_to_now(self) -> None:
        before_ms = int(time.time() * 1000)
        value = generate_uuid7()
        after_ms = int(time.time() * 1000)

        embedded = _timestamp_ms(value)
        assert before_ms <= embedded <= after_ms
