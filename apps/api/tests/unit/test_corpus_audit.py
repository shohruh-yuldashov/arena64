"""The corpus, audited as **files** — A64-014.9.

The other corpus suite asks whether the engine agrees with the
expectations. This one asks whether the expectations are well-formed: that
every file parses, that every entry is written in the encoding production
actually uses, that every reader a shape needs exists, and that the
append-only history is intact.

## Why re-serialization is the strongest check available

For every entry the audit reads it into domain values through the loader
and writes it back out through `engine.serialization` — the same functions
a stored game goes through — and compares against what is on disk.

A pass means three things at once: the file is valid, the loader and the
serializer agree, and the file is written in the **canonical** form. A
corpus that drifted from the serializer would be a contract nothing
enforced, and it would drift silently, because a loader lenient enough to
read a stale shape is a loader that hides the staleness.

AD-14 calls the corpus "the contract". This is the test that it is still a
contract and not a pile of JSON.
"""

import json
from collections.abc import Iterator, Mapping
from typing import Any

from app.modules.engine.serialization import (
    coordinate_from_primitive,
    move_from_primitive,
    move_to_primitive,
    position_to_primitive,
)
from app.modules.game.domain.serialization import (
    move_record_to_primitive,
    move_records_from_primitive,
)
from tests.corpus import (
    CORPUS_ROOT,
    EXPECTATION_KEYS,
    LATEST_VERSION,
    corpus_documents,
    load_cases,
    load_draw_sequences,
    load_rejections,
    load_replays,
    load_terminal_positions,
    position_of,
    superseded_ids,
)


def _moves_in(entry: Mapping[str, Any]) -> Iterator[Mapping[str, Any]]:
    """Every move written anywhere in an entry, whichever shape it is.

    The five expectation shapes spell a move identically and spell it in
    four different places — `expected_moves`, `move`, `moves`, and inside
    `records` — so the audit walks them all rather than knowing which is
    which.
    """
    for key in ("expected_moves", "moves"):
        yield from entry.get(key, ())
    if "move" in entry:
        yield entry["move"]
    for record in entry.get("records", ()):
        yield record["move"]


DOCUMENTS = corpus_documents()
ALL_LOADERS = (
    load_cases,
    load_rejections,
    load_terminal_positions,
    load_draw_sequences,
    load_replays,
)


class TestTheFilesParse:
    def test_the_corpus_is_not_empty(self) -> None:
        """A loader that silently found no files would make every
        conformance assertion in the suite vacuously true."""
        assert DOCUMENTS

    def test_every_file_declares_the_version_of_its_directory(self) -> None:
        """`corpus_documents` raises on a mismatch, so reaching here is the
        assertion. Stated anyway, because the guard is easy to delete."""
        for document, name in DOCUMENTS:
            assert document["corpus_version"] == int(name.split("/")[0].removeprefix("v")), name

    def test_every_file_says_what_it_covers(self) -> None:
        for document, name in DOCUMENTS:
            assert document["scope"].strip(), name

    def test_every_file_carries_exactly_one_expectation_shape(self) -> None:
        """A file that mixed shapes would make a reader guess which one it
        was looking at, and the loader skips a file for the key it does not
        understand — so a second key would be silently unread."""
        for document, name in DOCUMENTS:
            present = [key for key in EXPECTATION_KEYS if key in document]
            assert len(present) == 1, name

    def test_every_expectation_shape_is_exercised(self) -> None:
        """A shape nothing uses is a reader nothing checks. All five are
        in force: legal moves, rejections, terminal positions, draw
        sequences and replays."""
        used = {key for document, _ in DOCUMENTS for key in EXPECTATION_KEYS if key in document}

        assert used == set(EXPECTATION_KEYS)

    def test_every_entry_has_an_id_and_a_description(self) -> None:
        for document, name in DOCUMENTS:
            for key in EXPECTATION_KEYS:
                for entry in document.get(key, ()):
                    assert entry["id"].strip(), name
                    assert entry["description"].strip(), f"{name}#{entry['id']}"

    def test_every_loader_returns_something(self) -> None:
        for loader in ALL_LOADERS:
            assert loader(), loader.__name__


class TestTheFilesRoundTrip:
    """Every entry, read into domain values and written back out through
    **production** serialization, must read back as the same value.

    The comparison is by value rather than by bytes, and that is not a
    weakening. Some hand-written entries list their pieces in reading order
    — the mover first — while the serializer emits them sorted by square,
    and the two describe the same position. Demanding byte equality would
    force an edit to `v1`, which the corpus is append-only precisely to
    prevent.

    What the round-trip does catch is the failure that matters: a
    serializer that stopped understanding a written shape, or started
    writing one the loader cannot read.
    """

    def test_every_written_position_survives_a_round_trip(self) -> None:
        for document, name in DOCUMENTS:
            for key in EXPECTATION_KEYS:
                for entry in document.get(key, ()):
                    if "pieces" not in entry:
                        continue
                    original = position_of(entry)
                    reread = position_of(position_to_primitive(original))
                    assert reread == original, f"{name}#{entry['id']}"

    def test_the_serializers_output_is_a_fixed_point(self) -> None:
        """Writing what was read produces something that writes to itself.
        A serializer that normalised differently on the second pass would
        make two stores of one game differ."""
        for document, name in DOCUMENTS:
            for key in EXPECTATION_KEYS:
                for entry in document.get(key, ()):
                    if "pieces" not in entry:
                        continue
                    once = position_to_primitive(position_of(entry))
                    twice = position_to_primitive(position_of(once))
                    assert twice == once, f"{name}#{entry['id']}"

    def test_the_serializer_writes_squares_in_order(self) -> None:
        """The property the written files are *not* required to have, and
        everything the engine writes does: two identical boards must
        produce identical text, or a reader diffing two stored games sees
        differences that are not there."""
        for document, name in DOCUMENTS:
            for key in EXPECTATION_KEYS:
                for entry in document.get(key, ()):
                    if "pieces" not in entry:
                        continue
                    written = position_to_primitive(position_of(entry))
                    squares = [piece["square"] for piece in written["pieces"]]
                    assert squares == sorted(
                        squares, key=lambda name: coordinate_from_primitive(name)
                    ), f"{name}#{entry['id']}"

    def test_every_written_move_survives_a_round_trip(self) -> None:
        """One entry is exempt, and says so: the `malformed_move` rejection
        case holds a path that steps nowhere, and it exists precisely
        because building it must fail. `test_engine_corpus.py` is where
        that failure is the expectation."""
        for document, name in DOCUMENTS:
            for key in EXPECTATION_KEYS:
                for entry in document.get(key, ()):
                    if entry.get("rejection") == "malformed_move":
                        continue
                    for written in _moves_in(entry):
                        assert move_to_primitive(move_from_primitive(written)) == written, (
                            f"{name}#{entry['id']}"
                        )

    def test_every_replay_record_re_serializes(self) -> None:
        for document, name in DOCUMENTS:
            for entry in document.get("replays", ()):
                rebuilt = move_records_from_primitive(entry["records"])
                written = entry["records"]
                assert [move_record_to_primitive(record) for record in rebuilt] == written, (
                    f"{name}#{entry['id']}"
                )

    def test_every_file_is_valid_json_with_a_trailing_newline(self) -> None:
        """A file without one produces a diff on every edit that touches the
        last line, which is noise in exactly the review a rules change
        needs read carefully."""
        for _, name in DOCUMENTS:
            text = (CORPUS_ROOT / name).read_text(encoding="utf-8")
            json.loads(text)
            assert text.endswith("\n"), name


class TestTheHistoryIsIntact:
    def test_every_superseded_id_named_a_case_that_existed(self) -> None:
        """A supersession pointing at nothing is a rules change nobody can
        follow — the append-only promise is that the retired case is still
        there to read."""
        every_id = {
            entry["id"]
            for document, _ in DOCUMENTS
            for key in EXPECTATION_KEYS
            for entry in document.get(key, ())
        }

        assert superseded_ids() <= every_id

    def test_every_supersession_names_its_replacement_and_its_reason(self) -> None:
        for document, name in DOCUMENTS:
            for entry in document.get("supersedes", ()):
                assert entry["replaced_by"].strip(), name
                assert entry["reason"].strip(), name

    def test_a_supersession_only_retires_an_earlier_version(self) -> None:
        for document, name in DOCUMENTS:
            for entry in document.get("supersedes", ()):
                assert entry["version"] < document["corpus_version"], name

    def test_no_id_is_reused_across_the_whole_corpus(self) -> None:
        identifiers = [
            entry["id"]
            for document, _ in DOCUMENTS
            for key in EXPECTATION_KEYS
            for entry in document.get(key, ())
        ]

        assert len(set(identifiers)) == len(identifiers)

    def test_loading_an_earlier_version_still_works(self) -> None:
        """v1 is history and must stay readable, superseded cases included
        — unreadable history is no history."""
        assert load_cases(through=1)
        assert load_rejections(through=1)

    def test_the_latest_version_is_the_highest_directory_present(self) -> None:
        versions = {int(name.split("/")[0].removeprefix("v")) for _, name in DOCUMENTS}

        assert max(versions) == LATEST_VERSION
