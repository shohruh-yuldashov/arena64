"""The analytics taxonomy's contracts — A64-027.1.

A64-027.1 froze semantics and built no pipeline, so there is nothing here to
integration-test. What there is, is a vocabulary that a collector, an outbox
consumer and a year of queries will all depend on — and four properties of
it that must not drift, because each one fails silently.

    the allowlist is derived        a hand-maintained second list is how a
                                    client eventually gets to write
                                    `user_registered`
    the registry is total           an event nobody classified is an event a
                                    collector cannot decide about
    names follow the convention     a taxonomy spelled two ways is two
                                    taxonomies
    the document agrees             `analytics.md` §18 and the registry are
                                    one decision written twice

The last one is the reason this file is worth its length. The table in the
document is what somebody reads before writing a query; the registry is what
the code enforces. Neither is authoritative alone.
"""

import re
from pathlib import Path

import pytest

from app.platform.analytics import (
    CLIENT_EMITTABLE,
    DENIED_PROPERTY_NAMES,
    REGISTRY,
    EventName,
    Owner,
    Trust,
    is_client_emittable,
    spec_for,
)

#: The repository root is four levels up from `apps/api/tests/unit`.
DOCUMENT = Path(__file__).resolve().parents[4] / "docs" / "01-architecture" / "analytics.md"

#: The names a browser may submit. Written out rather than derived, so this
#: test disagrees with the code when the code changes — which is the only
#: way it can catch a name quietly becoming client-emittable.
EXPECTED_CLIENT_EMITTABLE = {
    EventName.LANDING_VIEWED,
    EventName.REGISTER_CTA_CLICKED,
    EventName.PUBLIC_TOURNAMENT_VIEWED,
    EventName.SHARE_CLICKED,
}


class TestTheRegistryIsComplete:
    def test_every_event_is_classified(self) -> None:
        """A name in the enum and absent from the registry is a name no
        collector can accept or reject, because it has no owner."""
        assert set(REGISTRY) == set(EventName)

    def test_every_entry_agrees_with_its_key(self) -> None:
        for name, spec in REGISTRY.items():
            assert spec.name is name

    def test_trust_follows_ownership(self) -> None:
        """The two are one fact. A backend event is a server truth and a
        frontend event is a browser's report; there is no third pairing."""
        for spec in REGISTRY.values():
            expected = Trust.AUTHORITATIVE if spec.owner is Owner.BACKEND else Trust.BEHAVIOURAL
            assert spec.trust is expected

    def test_every_event_starts_at_version_one(self) -> None:
        """Nothing has shipped, so nothing can have been bumped yet. When
        the first bump happens this test is the thing that makes somebody
        write down which event and why."""
        assert all(spec.version == 1 for spec in REGISTRY.values())


class TestClientsCannotWriteHistory:
    """The security boundary, asserted rather than reviewed."""

    def test_the_allowlist_is_exactly_the_frontend_events(self) -> None:
        assert CLIENT_EMITTABLE == EXPECTED_CLIENT_EMITTABLE

    @pytest.mark.parametrize(
        "name",
        [
            EventName.USER_REGISTERED,
            EventName.EMAIL_VERIFIED,
            EventName.MATCH_STARTED,
            EventName.MATCH_COMPLETED,
            EventName.RATING_CHANGED,
            EventName.TOURNAMENT_ENTERED,
            EventName.TOURNAMENT_COMPLETED,
            EventName.QUEUE_JOINED,
            EventName.MATCH_FOUND,
            EventName.CHALLENGE_RESOLVED,
        ],
    )
    def test_a_browser_may_not_claim_a_server_fact(self, name: EventName) -> None:
        """Named one by one, because the list is the thing being protected.

        A collector accepting any of these from a request body would let
        anybody write Arena64's own history — a fabricated registration, a
        match that never happened, a rating that never moved.
        """
        assert not is_client_emittable(name.value)

    def test_an_unknown_name_is_not_emittable(self) -> None:
        """The answer for a name outside the taxonomy is `False`, not an
        exception the endpoint has to remember to catch."""
        assert not is_client_emittable("definitely_not_an_event")
        assert not is_client_emittable("")

    def test_every_backend_event_is_excluded(self) -> None:
        backend = {name for name, spec in REGISTRY.items() if spec.owner is Owner.BACKEND}
        assert backend & CLIENT_EMITTABLE == set()


class TestNaming:
    def test_names_are_snake_case(self) -> None:
        for name in EventName:
            assert re.fullmatch(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*", name.value), name

    def test_names_are_unique(self) -> None:
        values = [name.value for name in EventName]
        assert len(values) == len(set(values))

    def test_names_read_as_facts_not_commands(self) -> None:
        """analytics.md §6: an event is something that happened.

        The imperative forms are the ones a reader reaches for by habit —
        `register_user` rather than `user_registered` — and a taxonomy that
        mixes the two makes every query author guess.
        """
        imperative_starts = ("create_", "send_", "join_", "start_", "complete_", "enter_", "make_")
        for name in EventName:
            assert not name.value.startswith(imperative_starts), name

    def test_an_interaction_is_never_named_for_the_fact_it_hopes_to_cause(self) -> None:
        """The distinction §6 exists to protect: what a person did is not
        what the system did. Clicking a button is not registering."""
        for name in CLIENT_EMITTABLE:
            assert name.value.endswith(("_clicked", "_viewed")), name


class TestThePrivacyDenylist:
    @pytest.mark.parametrize(
        "field",
        ["email", "username", "display_name", "ip_address", "user_agent", "bio", "avatar_url"],
    )
    def test_the_obvious_identifiers_are_denied(self, field: str) -> None:
        assert field in DENIED_PROPERTY_NAMES

    def test_a_hashed_email_is_denied_too(self) -> None:
        """Hashing is not anonymising — analytics.md §10.

        A hashed email is a stable identifier for a person and joins across
        any other system holding the same hash. It is on the list because it
        is the one somebody proposes in good faith.
        """
        assert {"email_hash", "hashed_email"} <= DENIED_PROPERTY_NAMES

    def test_free_text_carriers_are_denied(self) -> None:
        """No free text reaches analytics, in any property, ever."""
        assert {"message", "message_text", "body", "content", "error_message"} <= (
            DENIED_PROPERTY_NAMES
        )

    def test_high_cardinality_strings_are_denied(self) -> None:
        """§14: a tournament name answers no product question that
        `tournament_id` does not, and it is unbounded."""
        assert {"tournament_name", "name", "url", "referrer", "query_string"} <= (
            DENIED_PROPERTY_NAMES
        )

    def test_no_allowed_property_name_is_also_denied(self) -> None:
        """The dimensions the metrics actually read must not have been
        caught by a denylist entry written too broadly."""
        allowed = {
            "rated",
            "variant",
            "speed_class",
            "outcome",
            "termination_reason",
            "winner_side",
            "ply_count",
            "duration_ms",
            "waited_ms",
            "queue_type",
            "resolution",
            "reason",
            "placement",
            "surface",
            "mechanism",
            "status",
            "format",
            "capacity",
            "entrant_count",
            "origin",
            "match_id",
            "tournament_id",
            "utm_source",
            "utm_medium",
            "utm_campaign",
        }
        assert allowed & DENIED_PROPERTY_NAMES == set()


class TestTheDocumentAndTheCodeAgree:
    """`analytics.md` §18 is what somebody reads before writing a query.

    Prose and a registry that disagree is worse than either alone: the
    reader trusts the table, the collector trusts the code, and the
    difference only surfaces as a metric that counts nothing.
    """

    def test_the_document_exists_where_the_registry_says_it_does(self) -> None:
        assert DOCUMENT.is_file(), DOCUMENT

    def test_every_registered_event_appears_in_the_taxonomy_table(self) -> None:
        text = DOCUMENT.read_text(encoding="utf-8")
        missing = [name.value for name in EventName if f"`{name.value}`" not in text]
        assert missing == []

    def test_the_document_names_no_event_the_registry_lacks(self) -> None:
        """The direction that catches the likelier mistake: an event
        described in the document, agreed in review, and never registered.
        """
        text = DOCUMENT.read_text(encoding="utf-8")
        # The approved table only. §18's second table lists events that
        # deliberately do **not** exist — `move_made`, a generic
        # `page_view` — with the reason each was refused, and an absence
        # documented on purpose is not drift.
        table = text[text.index("## 18. Event taxonomy") : text.index("### Deliberately absent")]
        known = {name.value for name in EventName}
        # Only the rows: a backticked identifier in the leading column.
        described = {
            match.group(1)
            for match in re.finditer(r"^\| `([a-z][a-z0-9_]+)`\s+\|", table, re.MULTILINE)
        }
        assert described - known == set()


class TestSpecLookup:
    def test_it_returns_the_entry(self) -> None:
        assert spec_for(EventName.MATCH_COMPLETED).owner is Owner.BACKEND

    def test_an_unregistered_name_raises_rather_than_defaulting(self) -> None:
        """A default would classify an unknown event as *something*, and
        the safe-looking default — behavioural — is the one that would let
        it through a collector."""
        with pytest.raises(KeyError):
            spec_for("not_an_event")  # type: ignore[arg-type]
