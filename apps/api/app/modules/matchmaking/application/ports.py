"""The ports `matchmaking`'s use cases declare — AD-06 puts them in the
layer that *needs* them, so a service depends on a contract and never on
`SqlAlchemyQueueRepository`.

Three protocols, and the split between them is by capability rather than
by storage — the argument every port pair on this platform makes:

    QueueRepository        everything about a ticket in PostgreSQL
    RatingSnapshotProvider what number a ticket records at entry (QT-2)
    RecentOpponentProvider who these players have just played (A64-015.3)
    CooldownRepository     who may not queue yet, and until when (A64-015.5)
    QueueRetentionStore    deleting the tickets nobody owes anybody (§8)
    PendingMatchSink       where a realtime match offer goes (§4)

A64-015.6 adds two more, and both are **audit** surfaces rather than
operational ones — appended to, read by support, never on a request path:

    CooldownAuditRepository          why a player was barred (§3)
    ReconciliationTimelineRepository what recovery did to a ticket (§4)

The **pairwise block** port is deliberately not here either:
`friends.public.PairingExclusions` already answers it, and BL-2 is
`friends`' rule to enforce. Two ports for "who may not be paired" would be
two places the block graph is interpreted, and one of them would eventually
check a single direction.

The presence port is deliberately **not** declared here.
`users.public.PresenceProvider` already exists, `matchmaking` imports it
from that published surface (R-1), and a local re-declaration would be a
second contract for one fact — which is exactly how "who is online" ends up
with two implementations and one of them wrong. A64-014.1 says so directly:
reuse `presence:v1:`, do not create another online-player index.
"""

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Protocol
from uuid import UUID

from app.modules.game.public import ProductVariant
from app.modules.matchmaking.domain.challenge import Challenge
from app.modules.matchmaking.domain.cooldown import QueueCooldown
from app.modules.matchmaking.domain.cooldown_audit import CooldownRecord
from app.modules.matchmaking.domain.pending_match import PendingMatchOffer
from app.modules.matchmaking.domain.queue_pool import QueuePool
from app.modules.matchmaking.domain.queue_ticket import QueueSnapshot, QueueTicket
from app.modules.matchmaking.domain.reconciliation_timeline import ReconciliationEntry
from app.modules.rating.public import RatingSnapshot, SpeedClass


class QueueRepository(Protocol):
    """Storage for `QueueTicket` — one repository per aggregate root.

    Every method is designed for **more than one worker**, because the
    deployment AD-02 describes is several processes against one table and
    A64-014.1 requires the queue to support horizontal workers from the
    first release. `claim_due` is the method where that is not free; the
    rest are safe because they are single statements keyed on a player or a
    ticket.
    """

    async def enqueue(self, ticket: QueueTicket) -> QueueTicket:
        """Writes a new waiting ticket. Flushes, never commits
        (repositories.md §5.1).

        Raises `AlreadyQueued` when the partial unique index refuses a
        second live ticket. The service checks first to produce that error
        without a write; this is the guard that holds when two joins race,
        and it is the one that matters — QT-1 exists because a player with
        two tickets can be paired into two matches.
        """
        ...

    async def cancel(self, ticket: QueueTicket) -> bool:
        """Records a resolved ticket, **only if the row is still waiting**.

        Returns whether it applied. A compare-and-set rather than a blind
        `UPDATE`, and the race it closes is ordinary rather than exotic: a
        player with the queue open on two devices, or one who taps cancel as
        the expiry sweep commits. A blind write would let a cancellation
        overwrite an expiry — or, worse once A64-014.2 exists, overwrite a
        `matched` ticket whose match has already been created.

        `False` is not a failure. It means somebody else resolved the
        ticket first, which is what `QueueService.leave` reports as "you
        were not queued" — the honest answer, and the idempotent one.
        """
        ...

    async def by_id(self, ticket_id: UUID) -> QueueTicket | None:
        """One ticket, whatever its status — A64-015.5.

        The read a **requeue** needs, and the only method on this port that
        does not filter by liveness: the ticket being restored is `matched`
        and therefore terminal, which every other read here deliberately
        excludes.

        `None` for an id that no longer names a row — retention removed it,
        or it never existed. An ordinary outcome rather than an error: a
        `match_declined` event redelivered after the retention horizon is a
        thing that can happen, and the honest answer is "there is nothing
        left to restore".
        """
        ...

    async def active_ticket(self, player_id: UUID, *, now: datetime) -> QueueTicket | None:
        """The player's live ticket, or `None`.

        **Live means `waiting` and not yet due.** A ticket past its
        `expires_at` that the sweeper has not reached is reported as
        absent, deliberately: `expires_at` is the rule and the sweep is
        only the bookkeeping, so a player must never be told they are
        queued because a worker is a few seconds behind — nor blocked from
        re-queueing by one.

        `None` rather than raising: a player who is not queued is the
        ordinary case, and every caller branches on it.
        """
        ...

    async def queue_snapshot(self, *, pool: QueuePool, now: datetime, limit: int) -> QueueSnapshot:
        """One pool as it stands: its depth, and its oldest live tickets.

        The read A64-014.2's pairing scan runs, declared now so the shape
        the scan needs is settled before anything depends on a different
        one. `limit` bounds the tickets; the depth is a count over the same
        predicate, so a bounded read never reports a wrong number (CLAUDE.md
        §10.5, and `QueueSnapshot` on why the two are separate).

        **Not a lock and not a reservation.** Two scans over one pool see
        the same tickets, which is why QT-4's atomic claim exists and why
        `claim_due` below is a different method rather than a flag here.
        """
        ...

    async def claim_due(
        self, *, now: datetime, limit: int, claimed_by: str
    ) -> Sequence[QueueTicket]:
        """Takes up to `limit` tickets whose window has closed, oldest
        first, for this worker.

        **The one method that must be safe under concurrency**, and the
        contract is the outbox's: two workers calling this simultaneously
        receive disjoint sets. A64-014.1 requires the proven mechanism
        rather than a new one, so the implementation is
        `SELECT ... FOR UPDATE SKIP LOCKED` — see
        `SqlAlchemyQueueRepository.claim_due` for what the alternatives cost.

        `claimed_by` is diagnostic. Correctness comes from the row lock, not
        from a column: a lease recorded in a column has no way to detect a
        dead holder, which is why this table has no `claimed_by` at all and
        the identifier reaches the log line instead.

        The rows stay `waiting` — claiming is not a transition. `expire`
        below is what resolves them, in the caller's transaction, so a
        worker that dies between the two leaves tickets that the next sweep
        simply claims again.
        """
        ...

    async def expire(self, ticket_ids: Sequence[UUID], *, at: datetime) -> int:
        """Marks claimed tickets expired. **One statement per sweep**,
        whatever the batch size.

        Returns the number of rows actually updated, which is the count the
        log line reports — not `len(ticket_ids)`, which would claim a
        transition for a ticket somebody cancelled in between. The predicate
        carries the two live statuses as well as the id list, for that
        reason.
        """
        ...

    async def claim_pair(
        self, ticket_ids: Sequence[UUID], *, now: datetime
    ) -> Sequence[QueueTicket]:
        """Locks exactly the two tickets a scan selected, or takes neither
        — A64-015.3's atomic claim.

        **The second method that must be safe under concurrency**, and it
        reuses `claim_due`'s mechanism rather than inventing one:
        `SELECT ... FOR UPDATE SKIP LOCKED` over the two ids. A64-015.3 §7
        requires exactly this and forbids a distributed lock.

        Returns both tickets or **an empty sequence** — never one. A single
        locked ticket is not a claim on a pair, and returning it would hand
        the caller half a pairing to reason about. Whatever the loser
        skipped is still `waiting` and will be reconsidered by the next
        scan.

        `SKIP LOCKED` is what makes the loser *skip* rather than *wait*: two
        workers that both selected the same pair would otherwise serialise,
        and the second would then be holding a lock on tickets the first is
        about to reserve.

        The predicate is narrower than `claim_due`'s and every clause
        excludes a real row:

            id IN (...)          the two the engine chose
            status = 'waiting'   not already reserved by another worker,
                                 not cancelled, not expired, not matched
            expires_at > now     the window has not closed since the
                                 snapshot was taken

        **Claiming is not a transition**, exactly as it is not for the
        expiry sweep: the rows come back `waiting`, the lock lasts as long
        as the caller's transaction, and `reserve` below is what changes
        anything.
        """
        ...

    async def claim_stale_reservations(self, *, now: datetime, limit: int) -> Sequence[QueueTicket]:
        """Takes up to `limit` reservations that have stood past their
        `reserved_until`, oldest deadline first — A64-015.4 §9.

        **The third method that must be safe under concurrency**, and it
        reuses `claim_due`'s mechanism rather than inventing one:
        `SELECT ... FOR UPDATE SKIP LOCKED`. Two reconcilers calling this
        simultaneously receive disjoint sets.

        The rows stay `reserved` — claiming is not a transition — so a
        worker that dies before it decides what to do leaves tickets the
        next tick claims again. What the reconciler then writes depends on
        a fact this repository cannot see: whether `game` created a match
        for the ticket. See `PairingReconciliationService`.

        Distinct from `claim_due` even though both claim live tickets on a
        deadline, because the deadlines mean different things and produce
        different actions: a ticket past `expires_at` is a player who has
        waited long enough and is expired, while a ticket past
        `reserved_until` is a *pairing* that did not finish and may well go
        back into the queue. Collapsing them would make the recovery path
        expire people whose match creation merely crashed.
        """
        ...

    async def reserve(self, tickets: Sequence[QueueTicket]) -> bool:
        """Moves claimed tickets from `waiting` to `reserved`. All or
        nothing.

        Returns whether **every** ticket transitioned, and writes nothing
        when the answer is no. `False` means at least one row was no longer
        `waiting`, and the caller treats the whole pairing as lost — a
        half-reserved pair would leave one player invisible to every future
        scan with no match coming.

        All-or-nothing is the *statement's* property, not the caller's
        rollback — see `SqlAlchemyQueueRepository._transition` on the guard
        subquery that makes it so.

        Compare-and-set on `status = 'waiting'`, like `cancel`, and for the
        same reason: the row lock from `claim_pair` makes this safe within
        one transaction, and the predicate is what makes it safe if the two
        are ever separated.

        Writes `reserved_until` from the tickets, which carry the deadline
        the caller computed — see `QueueTicket.reserved`.
        """
        ...

    async def release(self, tickets: Sequence[QueueTicket]) -> bool:
        """Returns reserved tickets to `waiting` — A64-015.3's compensation.

        Called when `game` refused the match after both tickets were
        reserved. Returns whether every one applied; `False` means somebody
        else resolved a reserved ticket, which is a state this system should
        not be able to reach and is therefore logged as an error rather than
        retried.

        **Writes `status` and `resolved_at` only.** `entered_at` is not in
        the statement at all, so a released player keeps the place in line
        they held — losing it to a failure that was the platform's would be
        a second penalty for the same fault.

        Compare-and-set on `status = 'reserved'`: a ticket the expiry sweep
        took because its window closed mid-pairing must not be resurrected
        into `waiting` past its own deadline.
        """
        ...

    async def complete(self, tickets: Sequence[QueueTicket], *, at: datetime) -> bool:
        """Moves reserved tickets to `matched`. All or nothing.

        Called **after** `game` has accepted the match request, never
        before — A64-015.3 §8. `at` is the instant the match was created.

        Returns whether every one applied. `False` after a successful match
        creation is the one genuinely bad outcome this module has: a match
        exists whose tickets do not say so. It is logged with the match id
        at `ERROR`, because the reconciliation is manual until A64-015.4
        gives a match a durable link back to its tickets.

        Compare-and-set on `status = 'reserved'`, so a ticket that was never
        reserved cannot be marked matched by a stray call.
        """
        ...


class RatingSnapshotProvider(Protocol):
    """What rating a ticket records when the player joins — QT-2.

    A port with one implementation that returns a constant, and it is worth
    saying plainly why that is not a stub.

    QT-2 is a *correctness* rule: "pairing must be deterministic within a
    tick; a rating changing mid-scan would make the same scan pair
    inconsistently." The rule is about the ticket carrying a snapshot rather
    than a reference, and it is fully implemented — the column exists, the
    aggregate holds it, the event carries it. What does not exist is a
    rating *system* (domain-model.md Q-3 is open, and `rating` is an unbuilt
    module), so the only honest snapshot today is the provisional starting
    value.

    That day arrived with A64-017.2: `rating.public.RatingReader` satisfies
    this port, and no use case, no aggregate and no test changed — which is
    what the port was for. What *did* change is the return type, from an
    `int` to the Glicko-2 triple, because ADR-001 made a rating a triple and
    §7.6's seat snapshot needs all three.
    """

    async def rating_for(
        self, player_id: UUID, *, variant: ProductVariant, speed_class: SpeedClass
    ) -> RatingSnapshot:
        """The player's rating in the key they are queueing under.

        Per key rather than per player, because that is what a rating is:
        `RatingKey` splits `(variant, speed class)`, and a single number
        would have to pick one.

        A64-020.5A-pre widened this from `queue_type`, which is exactly the
        change the previous signature predicted — "`QueueType` is the axis
        this task has; when time controls arrive the argument widens and
        callers do not". It widened to the *key's own two components* rather
        than to a pool, so this port states what a rating is keyed by and not
        how a queue happens to be shaped.

        `queue_type` is gone rather than kept, and its absence is the point:
        ranked and casual rate against the same key, so an argument that was
        accepted and deliberately ignored was an invitation to start using
        it. Whether a *result* counts is `rated` on the match (SPEC-RATING
        §9), and it always was.

        **The whole triple**, not just the value. The ticket records only
        the value (QT-2 — pairing sorts on one number), but the same read
        feeds the seat snapshot at match creation, and PR-3 requires the
        rating calculation to run on the deviation and volatility captured
        then. A port that returned an `int` would make PR-3 unimplementable,
        and the failure would appear as two concurrent matches computing
        against each other's partial results.

        Never raises and never returns `None`. A player with no measured
        rating has the provisional starting triple (PR-6), which is a value
        rather than an absence — and a join that failed because a rating
        could not be read would take matchmaking down for a number that has
        a safe default.
        """
        ...


class RecentOpponentProvider(Protocol):
    """Who each of these players has just finished a game against —
    A64-015.3 §6.

    The rematch guard. Being handed the same opponent twice in a row is the
    most common complaint about a thin pool, and the exclusion is cheap to
    apply and awkward to retrofit into a scan that was not designed for it
    — which is why the port exists now and the implementation does not.

    ## Why this is declared here and not imported from `game.public`

    AD-06: a port is declared by the layer that needs it. A64-015.3 said
    this would be "satisfied by `game.public` ... and no use case, no
    engine and no test changes", and A64-015.4 is where that happened:
    `game.public.RecentOpponentReader` has the same shape, so
    `GameRecentOpponents` satisfies *this* protocol structurally and the
    composition root wires one object with no adapter between them.

    The two are not a duplication. This states what the pairing scan needs
    and belongs to the module that needs it; the other states what `game`
    is prepared to answer and belongs to the module that publishes it.
    Collapsing them would make every `PairingService` test depend on `game`,
    including the ones that have no matches at all.

    **The prediction that the no-op could be swapped for one line held**:
    `NoRecentOpponents` is gone, `build_recent_opponents` names `game`'s
    classes instead, and `PairingService` is unchanged.
    """

    async def recent_opponents_among(
        self, player_ids: Sequence[UUID]
    ) -> Mapping[UUID, frozenset[UUID]]:
        """For each of `player_ids`, which **others in the same batch** they
        have just played.

        The same shape as `friends.public.PairingExclusions`, deliberately:
        both are "pairs to veto", `PairExclusions.merged` unions them, and
        one shape means the engine asks one question rather than two.

        Batch and symmetric, for the same reasons — a per-candidate form
        would be an N+1 inside a scan that runs continuously, and "they
        played me" and "I played them" are the same game.

        Never raises. An unreadable history must degrade to "no exclusions"
        rather than stop pairing: a rematch is a disappointment, and an
        empty pool is an outage.
        """
        ...


class CooldownRepository(Protocol):
    """Storage for `QueueCooldown` — A64-015.5 §3.

    Its own port rather than three methods on `QueueRepository`, and the
    split is by *capability* like every port pair on this platform: the
    pairing scan and the expiry sweep both hold a queue repository, and
    neither has any business being able to bar somebody from the queue.

    **Durable, never process-local.** §3 forbids in-memory enforcement, and
    the reason is the deployment AD-02 describes: several processes against
    one database. A cooldown held in one worker's dictionary is a cooldown
    the next request routes around.
    """

    async def apply(self, cooldown: QueueCooldown) -> QueueCooldown:
        """Records a cooldown, **extending** any the player already has.

        Returns the cooldown that is now in force, which is not necessarily
        the one passed in: a player who declines twice keeps whichever
        window ends later, and the caller needs the stored answer to report
        an honest `retry_after`.

        One statement — an upsert on `player_id` whose `SET` takes the
        later expiry — because the alternative is read-then-write, and two
        declines landing together would then both read "no cooldown" and
        the second would overwrite rather than extend. That is exactly the
        "repeated decline does not bypass the cooldown" rule §3 states, and
        it is a constraint under concurrency rather than an `if`.

        Flushes, never commits (repositories.md §5.1): the cooldown and the
        outbox row that records why it exists are one transaction.
        """
        ...

    async def active_for(self, player_id: UUID, *, now: datetime) -> QueueCooldown | None:
        """The cooldown barring this player, or `None`.

        **Expiry is applied in the query**, exactly as `active_ticket`
        applies `expires_at`: a lapsed row that retention has not reached
        yet must read as absent, or a player would be refused by
        bookkeeping rather than by a rule.

        `None` rather than raising — a player with no cooldown is the
        overwhelmingly common case and every caller branches on it.
        """
        ...

    async def prune_expired(self, *, before: datetime, batch_size: int) -> int:
        """Deletes lapsed cooldown rows. Returns how many went.

        Retention for the one relation on this platform whose rows are
        *worthless* once expired: a cooldown that has lifted answers no
        question anybody will ask, unlike a resolved queue ticket ("why was
        I matched with them") or a settled match (the permanent record).

        Bounded and safe for more than one pruner, by the same
        `SKIP LOCKED` the outbox's retention uses.
        """
        ...


class QueueRetentionStore(Protocol):
    """Deleting the queue history nobody owes anybody — A64-015.5 §8.

    A **separate port from `QueueRepository`**, and the split is the one
    `OutboxRetentionStore` already makes against `OutboxRepository`: the
    queue's use cases can enqueue, claim, reserve and resolve a ticket; they
    must not be able to *delete* one. A bug in the expiry sweep that reached
    a `DELETE` would destroy the history "why was I matched with them" is
    answered from.

    Satisfied by an adapter constructed only by the retention job's own
    session — nothing on the HTTP path holds it.
    """

    async def prune_resolved(self, *, before: datetime, batch_size: int) -> int:
        """Deletes up to `batch_size` **terminal** tickets resolved before
        `before`. Returns how many rows went.

        **Never touches a live ticket**, whatever its age. `waiting` and
        `reserved` are excluded by predicate rather than by the horizon,
        because a reserved ticket stranded by a dead worker is *old* and is
        precisely the row reconciliation still needs — deleting it would
        turn a recoverable pairing into a player who is silently no longer
        in any queue.

        The cutoff is `resolved_at`, which is non-null exactly for terminal
        rows (`ck_queue_ticket__resolved_iff_terminal`), so the predicate and
        the horizon agree by construction rather than by review.

        Bounded by `batch_size` and safe for more than one pruner, by the
        same `FOR UPDATE SKIP LOCKED` the relay's claim uses. An unbounded
        `DELETE` on the queue relation would be an incident of its own.
        """
        ...

    async def live_before(self, instant: datetime) -> int:
        """How many **live** tickets are older than `instant`.

        Not used to decide anything. It is the number that says *why* the
        floor did not move, and on this relation it is a genuine alarm
        rather than bookkeeping: a `waiting` ticket older than the whole
        retention horizon is a player who has been in a queue for days,
        which means the expiry sweep has stopped. `PruneResult` carries it
        for the same reason `retained_unpublished` carries the outbox's.
        """
        ...


class PendingMatchSink(Protocol):
    """Where a realtime match offer goes — A64-015.5 §4.

    The seam AD-09's gateway fills. Today's implementation writes a log
    line, exactly as `notifications.NotificationSink` does and for the same
    reason: there is no WebSocket transport in this build, and A64-015.5
    excludes building one ("Do not implement the full live-game WebSocket
    protocol").

    **This is a seam, not a stub**, and the distinction is that everything
    upstream is real — the offer was made durable in the same transaction as
    the match, the relay claimed it, the participant and the deadline were
    re-read at delivery, and the opponent preview passed the privacy gate.
    What is missing is only the socket.

    ## Its own port rather than `notifications.NotificationSink`

    That one carries a `SocialNotification`, which is a rendered
    `PublicProfile` about a *subject* — the wrong shape for a match offer,
    which is about a contest and carries a deadline the client must count
    down. AD-06 puts a port in the layer that needs it, and reusing a
    contract by widening it is how one type ends up meaning two things.

    A sink **may raise**. A delivery that failed is one the platform should
    retry, so the exception propagates to the consumer, which turns it into
    a recorded per-event failure and a backoff. Swallowing it here would
    mark the event published and lose the offer silently.
    """

    async def deliver(self, offers: Sequence[PendingMatchOffer]) -> None:
        """Delivers a batch. An empty batch is a legal no-op.

        **Batch-first**, like `EventHandler` and `NotificationSink`: the
        consumer resolves a whole relay tick at once, and a singular method
        would be called in a loop by every implementation.
        """
        ...


class CooldownAuditRepository(Protocol):
    """Storage for `CooldownRecord` — A64-015.6 §3.

    Its own port rather than methods on `CooldownRepository`, and the split is
    the one every port pair on this platform makes: what differs is the
    capability. The eligibility check holds the enforcement store and must not
    be able to write history; the policy that applies a cooldown writes both.

    **Append-only.** There is no `update` and no `delete` beyond the retention
    sweep, which is what makes a row here evidence rather than a current
    opinion.
    """

    async def record(self, entry: CooldownRecord) -> CooldownRecord:
        """Writes one audit row, or returns the one already written for this
        `(player_id, source_match_id)`.

        **Idempotent by unique index**, not by check-then-insert: the caller
        is an outbox consumer under an at-least-once contract, so a
        redelivered decline reaches it twice by design. `ON CONFLICT DO
        NOTHING` followed by a read is what makes the second call a no-op
        rather than a second row — and §3 requires exactly that ("duplicate
        processing must not create conflicting records").

        Flushes, never commits (repositories.md §5.1): the audit row and the
        enforcement row are one transaction, because a bar with no record of
        why is the thing this port exists to prevent.
        """
        ...

    async def history_for(self, player_id: UUID, *, limit: int) -> Sequence[CooldownRecord]:
        """This player's cooldowns, most recent first.

        The support query §3 asks for, bounded by `limit` because every list
        read on this platform is (CLAUDE.md §10.5). It reads the audit
        relation rather than the enforcement one, so it still answers after
        the bar has lifted — which is when the question is actually asked.

        **No route reaches this.** See `CooldownRecord` on why the audit trail
        is operational rather than a product surface.
        """
        ...

    async def prune_recorded(self, *, before: datetime, batch_size: int) -> int:
        """Deletes audit rows applied before `before`. Returns how many went.

        Bounded and safe for more than one pruner, by the same
        `FOR UPDATE SKIP LOCKED` every delete on this platform uses.

        The cutoff is `applied_at` rather than `expires_at`: the horizon is
        "how long is a dispute answerable", which runs from the event and not
        from when the bar happened to lift.
        """
        ...


class ReconciliationTimelineRepository(Protocol):
    """Storage for `ReconciliationEntry` — A64-015.6 §4.

    A projection, so this port is deliberately narrower than a repository for
    an aggregate: append, two reads, and a bounded delete. Nothing can amend
    an entry, because an amended timeline is not one.
    """

    async def append(self, entry: ReconciliationEntry) -> ReconciliationEntry:
        """Writes one entry, or returns the one already written for its
        `event_id`.

        **Idempotent by unique index on `event_id`**, for the reason the
        cooldown audit is: the caller is an outbox consumer, and AD-16
        guarantees it will see some events twice. §4 requires idempotent
        consumption, and a constraint is the only form of that which holds
        when two relays deliver concurrently.
        """
        ...

    async def for_ticket(self, ticket_id: UUID, *, limit: int) -> Sequence[ReconciliationEntry]:
        """Everything recovery did to one queue ticket, most recent first.

        The lookup a support conversation starts from — §4 requires the
        timeline to be "queryable by ticket or pairing identifier", and the
        ticket is the half that is always populated.
        """
        ...

    async def for_pairing(self, pairing_id: UUID, *, limit: int) -> Sequence[ReconciliationEntry]:
        """Everything recovery did to one pairing, most recent first.

        **Returns nothing today**, and the column it reads is nullable and
        always null — see `ReconciliationEntry.pairing_id`. The method exists
        because §4 names the query and because a caller written against it now
        needs no change when `PairingReconciled` starts carrying a pairing
        id; returning an empty sequence is the honest answer to "which
        recovery actions belonged to this pairing" when nothing records the
        association.
        """
        ...

    async def prune_recorded(self, *, before: datetime, batch_size: int) -> int:
        """Deletes entries that occurred before `before`. Returns how many
        went.

        The cutoff is `occurred_at`, so the timeline's floor lines up with the
        outbox entries it was projected from rather than with when the relay
        happened to catch up.
        """
        ...


class ChallengeRepository(Protocol):
    """Storage for friend challenges — A64-022.1 §15.

    Declared here because the port belongs to the layer that needs it
    (AD-06), and deliberately **narrow**: four methods, each one a use case's
    question. There is no `list_pending_for`, because nothing in this phase
    reads a list — that arrives with the API in A64-022.2, and a method
    written now would be a method nothing exercised.

    ## Every read is scoped

    There is no `get(challenge_id)`. A reader that could fetch a row by id
    alone is one line away from serving a challenge between two strangers,
    and the actor is not a filter applied afterwards — it is half the
    question (§21's IDOR rule).
    """

    async def add(self, challenge: Challenge) -> None:
        """Stores a new pending challenge.

        Raises `ConflictError` when the pair already has a live one. The
        rule is `uq_friend_challenge__live_pair`, a partial unique index over
        the *unordered* pair — so this is an insert that may be refused
        rather than a check followed by an insert, and two simultaneous
        creates in opposite directions produce one row and one conflict
        instead of two winners of a race neither could see.
        """
        ...

    async def get_for_party(self, challenge_id: UUID, *, party_id: UUID) -> Challenge | None:
        """One challenge, **scoped to somebody who is part of it**.

        `party_id` matches either side: both the challenger and the recipient
        may act on a challenge, and which of them may do *what* is the
        aggregate's rule rather than this one's.

        `None` for a challenge that does not exist **and** for one between
        two other people, deliberately indistinguishable — an id that
        answered differently for a stranger would be an existence oracle.
        """
        ...

    async def save(self, challenge: Challenge) -> None:
        """Writes a settled challenge back.

        Takes the whole aggregate rather than a status, because the
        transition already produced one and passing the parts would let a
        caller write a terminal status with no `responded_at`.
        """
        ...

    async def list_for_party(
        self,
        party_id: UUID,
        *,
        as_challenger: bool,
        now: datetime,
        limit: int,
        cursor: str | None,
    ) -> tuple[Sequence[Challenge], str | None]:
        """One keyset page of this player's **live** challenges — A64-022.2 §7.

        `as_challenger` chooses the direction: outgoing when `True`, incoming
        when `False`. One method rather than two, because the two differ in a
        single predicate and a second method would be a second place the
        ordering, the tiebreak and the over-fetch could drift.

        **Live means pending and unexpired**, which is why `now` is a
        parameter rather than a database `now()`: expiry is decided by the
        platform's injected clock (AD-07), and a query that read the server's
        wall clock would disagree with the aggregate that refuses an answer.

        A64-022.2 exposes no history. A terminal challenge leaves these lists
        silently and is not deleted — the row is the record that an
        invitation happened, and a history endpoint is a product decision
        nobody has taken.
        """
        ...

    async def find_live_between(self, first: UUID, second: UUID) -> Challenge | None:
        """The live challenge between these two, whichever direction.

        Unordered, matching the constraint. Its purpose is a **message**, not
        a guard: `add` is what actually enforces the rule, and this exists so
        the service can answer "you already have one with them" instead of
        surfacing a conflict the caller cannot interpret.
        """
        ...
