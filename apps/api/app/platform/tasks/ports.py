"""The task-dispatch ports — AD-17's seam, declared before Celery exists.

AD-17 names Celery as the platform's only asynchronous execution framework
and says the migration cost is "only the relay's dispatch adapter is
replaced". That claim is currently unfalsifiable, because nothing on the
platform *dispatches* anything: the relay calls a handler, and the presence
sweeper calls a service. Both are function calls wearing a worker's clothes.

These three protocols are the seam that makes the claim checkable. A caller
that wants background work done constructs a `TaskRequest` and hands it to a
`TaskDispatcher`; it does not know whether the work will run in this event
loop or on another machine, and it must not be able to find out.

    TaskRequest     what to run, where, with what — JSON-shaped throughout
    TaskDispatcher   accepts a request. The half Celery replaces
    TaskHandler      what runs. The half Celery does not touch

## The contract is the weaker of the two implementations, deliberately

`InlineTaskDispatcher` completes the work before `dispatch` returns.
`CeleryTaskDispatcher` will return the moment the broker accepts the
message. If the contract were written to the inline behaviour, every caller
would quietly come to depend on the effect having happened by the time
`dispatch` returned — and the Celery migration would then be a behaviour
change in every one of them rather than an adapter swap.

So the contract says only: **the request has been accepted for execution.**
Nothing about when it runs, nothing about whether it succeeded, and no
return value to inspect. A caller that needs an answer is not dispatching a
task; it is making a call, and should make one.

## Why the payload is JSON-shaped and not an object

A `TaskRequest` crosses a process boundary the moment the dispatcher is
Celery's, and at that point the payload is serialised by a broker that knows
nothing about this codebase's types. Declaring it as primitives now means the
migration cannot be blocked by a request nobody can encode — and it forces
the same discipline `DomainEvent.payload` already applies: a task carries
what its handler needs, not a reference to state that may have moved on.

## Why this is `app/platform` and not a module

Same rule the outbox follows (`app/platform/__init__.py`): every module will
eventually want background work, so a dispatcher owned by whichever module
needed one first would make every other module import it. Nothing here
imports `app.modules`, and `.importlinter` fails if that changes.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

#: The queue a request is routed to when it names none.
#:
#: A string rather than an enum, because the set of queues is a *deployment*
#: fact — AD-20 separates workers by service-level objective, and which SLO
#: classes exist is decided by how the platform is run rather than by this
#: file. Celery's `queue=` argument takes exactly this.
DEFAULT_QUEUE = "default"


@dataclass(frozen=True, slots=True)
class TaskRequest:
    """One unit of background work, described rather than performed.

    Frozen, and every field is something a broker can carry: this is the
    value that becomes a Celery message body, so anything unencodable here
    is a migration that fails at the last moment.
    """

    name: str
    """The handler's stable name — `matchmaking.queue.expire`.

    Namespaced by owner, exactly as `DomainEvent.event_type` is, so two
    contexts cannot collide on a bare verb and an operator reading a queue
    can tell who asked for the work.

    Renaming one is a deployment concern rather than a rename: a scheduler
    on the new name and a worker on the old one dispatch into silence.
    """

    queue: str = DEFAULT_QUEUE
    """Which worker pool should run it — AD-20's SLO isolation.

    Carried today by a dispatcher that has exactly one pool, and therefore
    only logged. It is on the request rather than inferred from the name
    because routing is the property AD-20 makes load-bearing ("separate
    queues with separate scaling make that class of interference
    structurally impossible"), and a field added later would have to be
    back-filled at every call site at once.
    """

    payload: Mapping[str, Any] = field(default_factory=dict)
    """The handler's arguments, as JSON primitives.

    Self-contained, for the reason `DomainEvent.payload` is: by the time a
    task runs, the row that caused it may have moved on. A payload holding
    only an id is one whose handler has to re-read state that may no longer
    say what the dispatcher saw.
    """


class TaskDispatcher(Protocol):
    """Accepts work for execution somewhere. The half AD-17 replaces.

    Held by schedulers and by services that need something done off the
    request path. It has one method and no way to ask about a result, which
    is what stops a caller depending on the inline implementation's timing.
    """

    async def dispatch(self, request: TaskRequest) -> None:
        """Accepts `request` for execution.

        **Returns when the request has been accepted, not when it has been
        performed**, and a caller must not assume otherwise even against an
        implementation where the two coincide.

        **Does not raise for a failure of the work itself.** A handler that
        blows up is the dispatcher's problem to record, not the caller's to
        catch — a scheduler that had to handle a prune failing would be a
        scheduler that knows what pruning is.

        It *does* raise for a failure to accept: an unroutable name is a
        wiring defect, and a dispatcher that swallowed one would leave a
        scheduled job silently doing nothing forever.
        """
        ...


class TaskHandler(Protocol):
    """What actually runs. The half Celery does not touch.

    A handler is a plain object with a name and a coroutine, so the same
    class is driven by the inline dispatcher today, by a Celery task
    tomorrow, and directly by a test in both cases.
    """

    @property
    def name(self) -> str:
        """The name this handler answers to — matched against
        `TaskRequest.name`.

        A property rather than a constructor argument, for the reason
        `EventHandler.consumer` is one: two instances of one handler cannot
        then be registered under two different names.
        """
        ...

    async def run(self, payload: Mapping[str, Any]) -> None:
        """Performs the work.

        Raising is legal and is how a handler reports failure — the
        dispatcher decides what that means. Returning `None` is the only
        success: a task has no caller waiting for a value.
        """
        ...
