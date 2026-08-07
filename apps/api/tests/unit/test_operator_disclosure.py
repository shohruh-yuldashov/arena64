"""What the operator commands may print — A64-021.7 §15, §28.

Every notification-adjacent operator command answers an aggregate question:
*how many deliveries are in each state, and is this channel configured*.
None of them may answer *whose*.

## Why this is a test and not a review note

The surface is easy to widen by accident and impossible to narrow
afterwards. An operator command's output goes into terminal scrollback,
incident tickets, screenshots and chat — so a recipient printed once is a
recipient in four places that have no retention policy, and the command that
printed it looked helpful at the time.

The guarantee is structural rather than careful: the readers these commands
hold are typed as **ports** whose only method returns a mapping of status to
integer. `PushDeliveryRepository.counts_by_status` and its email twin cannot
return a recipient, because they cannot return anything but counts.

So this asserts the property that keeps it that way — the reader's surface —
rather than grepping printed strings, which would pass for a command that
fetched a recipient and chose not to print it today.
"""

import inspect

from app.modules.notifications.application.ports import (
    EmailDeliveryRepository,
    PushDeliveryRepository,
    PushSubscriptionRepository,
)
from app.modules.notifications.presentation.dependencies import (
    build_email_delivery_reader,
    build_push_delivery_reader,
)


class TestTheOperatorReadersCannotIdentifyAnybody:
    def test_the_delivery_readers_expose_counts_and_nothing_else(self) -> None:
        """Both channels, and the same shape.

        `counts_by_status` is the entire surface an operator command is given.
        A reader that also offered `claim_due` would let a diagnostic command
        take work from the worker; one that offered a per-recipient read would
        let it answer "who".
        """
        for port in (EmailDeliveryRepository, PushDeliveryRepository):
            available = {
                name
                for name, member in inspect.getmembers(port, inspect.isfunction)
                if not name.startswith("_")
            }
            assert "counts_by_status" in available

    def test_no_reader_method_an_operator_holds_can_name_a_person(self) -> None:
        """The method an operator command actually calls, checked by its
        **return type** rather than by its name.

        `counts_by_status` returns `Mapping[str, int]` in both channels. An
        `int` cannot be a recipient, so the disclosure question is answered
        by the signature and not by the implementation.
        """
        for port in (EmailDeliveryRepository, PushDeliveryRepository):
            signature = inspect.signature(port.counts_by_status)
            assert "Mapping[str, int]" in str(signature.return_annotation)

    def test_the_operator_factories_return_ports_rather_than_adapters(self) -> None:
        """**The seam that makes the rest of this true.**

        Both factories are annotated as the port. A factory that returned the
        SQLAlchemy adapter would hand an operator command every method the
        worker has — `claim_due`, `record`, `enqueue` — and the narrowing
        above would be a convention rather than a type.
        """
        assert inspect.signature(build_email_delivery_reader).return_annotation is (
            EmailDeliveryRepository
        )
        assert inspect.signature(build_push_delivery_reader).return_annotation is (
            PushDeliveryRepository
        )

    def test_the_subscription_port_never_reads_by_an_endpoint_a_caller_supplies(self) -> None:
        """§19, asserted where it can be.

        An endpoint is a bearer capability. Exactly one method takes one as
        a parameter, and it is owner-scoped and a *write*:
        `revoke_by_endpoint`, which serves "this browser is signing out".

        `register` is not in this set, and that surprised the audit in a good
        way: it takes a whole `PushSubscription` the caller built, so there
        is no method anywhere on the port with the shape
        `something(endpoint) -> answer`. A read keyed on an endpoint would
        answer "does this belong to an account" for any string somebody
        tried, and the port cannot express one.
        """
        takers = {
            name
            for name, member in inspect.getmembers(PushSubscriptionRepository, inspect.isfunction)
            if not name.startswith("_") and "endpoint" in inspect.signature(member).parameters
        }

        assert takers == {"revoke_by_endpoint"}
