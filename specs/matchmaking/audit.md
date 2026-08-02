# Matchmaking — Audit and Operational Hardening

> **Status:** Complete — A64-015.6, the closing task of the Matchmaking Epic
> **Owner:** _Unassigned_
> **Audited:** 2026-08-02
> **Scope:** `apps/api/app/modules/matchmaking/`, the `game` surfaces matchmaking consumes,
> `apps/api/app/platform/outbox/`, `apps/api/app/platform/metrics/`, `specs/matchmaking.md`
> **Related:** `specs/matchmaking.md` §12, `specs/game-engine/audit.md`,
> architecture.md AD-06, AD-07, AD-16, AD-17, AD-19

## Readiness

# READY WITH DOCUMENTED LIMITATIONS

Every path a live game needs is implemented, specified, and covered by tests that run the real
services: a player joins a queue, a scan pairs them, `game` creates a match, both sides answer,
and every way that sequence can fail has a defined recovery. Nothing found in this audit is a
correctness defect in that path.

Four limitations are load-bearing rather than cosmetic. Each is a decision, and each is recorded
here with what it costs:

1. **The thirty-second acceptance deadline is still an assumption** (§2). It is now a
   *measurable* one — the histogram exists and passes through the metrics pipeline at full
   fidelity — but no production data has been collected, so the number has not been validated
   against how long players actually take.
2. **The outbox's retry budget is per entry, not per consumer** (§5). A consumer that fails one
   entry consistently spends that entry's shared attempts, so a poison consumer can exhaust the
   budget for its healthy neighbours on the same entry. Bounded in practice by every consumer
   being idempotent and reporting per-entry failures rather than raising.
3. **The realtime sink is still a log line** (§6). `LoggingPendingMatchSink` is wired where
   AD-09's gateway will go; everything upstream of the socket is real, and clients fall back to
   polling, which is specified and tested.
4. **Metrics are emitted as structured log records** (§7). There is no Prometheus, StatsD or
   OpenTelemetry collector in this deployment, and adding one is outside a task's authority. The
   port, the call sites, the labels and the aggregation are all real; only the exporter is not.

None blocks live game integration. Items 1 and 2 are the two worth revisiting first, in that
order.

---

## 1. Documentation merge audit

**Finding: the content is complete and correct today; the process that produced it failed once.**

`git show --name-only 71bab6a` — the A64-015.4 commit merged as PR #40 — contains **no files
outside `apps/api/`**. Every documentation file that task wrote was left out of its own commit.
The content arrived one commit later, swept into A64-015.5's `1091c05`, which did carry
`specs/matchmaking.md`, `docs/01-architecture/architecture.md`, `database.md` and
`domain-model.md`.

The root cause is mechanical: `git add .` run from inside `apps/api/` stages only that subtree.
The fix is `git add -A` from the repository root, and it is a habit rather than a code change.

State after this audit, verified rather than assumed:

| Document | Covers | Status |
| --- | --- | --- |
| `specs/matchmaking.md` §1–§11 | A64-014.1 through A64-015.5 | Present, no duplication found |
| `specs/matchmaking.md` §12 | A64-015.6 | Added by this task |
| `database.md` §8.1a, §8.1b | `queue_ticket`, `queue_cooldown` | Present |
| `database.md` §8.1c, §8.1d | `queue_cooldown_audit`, `pairing_timeline` | Added by this task |
| `database.md` §8.2a | `game.match` as it ships | Present |

No contradictions were found between the spec and the implementation. Two numbering artefacts
were corrected: the old §12 became §13, and the status line now reads §1–§13.

**Tripwire.** `tests/unit/test_matchmaking_audit.py::TestTheDocumentationDescribesWhatShipped`
asserts that every task in the epic has a section and that every relation the module owns appears
in the database document. A relation that ships undocumented now fails a test rather than being
noticed by whoever reads the migration next.

---

## 2. The acceptance deadline — evidence, not intuition

**Finding: unchanged at thirty seconds, and deliberately so.**

`MATCHMAKING_RESERVATION_TTL_SECONDS` is 30 and this task did not move it. The brief forbids
changing it on intuition, and the honest state of the evidence is:

| What exists | What it shows |
| --- | --- |
| `game.match_answer_latency_seconds{outcome}` | How long players actually take to answer, as a distribution, since A64-015.5 |
| `matchmaking.acceptance_failure_actions_total{action}` | How often the window closed with no answer versus a decline |
| Production data | **None.** No deployment has run |

So the histogram is the mechanism and there is nothing in it yet. What A64-015.6 added is the
guarantee that the mechanism survives the metrics work: `AggregatingMetrics` sums **counters** and
passes **observations** through untouched, precisely so this measurement keeps the distribution a
`p99` is read from. A mean and a count would answer neither "how long does the median player take"
nor "how long does the slowest tenth take", and the second is the one the deadline is about.

One structural property was verified and is worth stating: **there is still one number**.
`reservation_ttl_seconds` is the reservation deadline, the acceptance deadline written to
`game.match`, and the instant written to both reserved tickets — computed once in
`PairingService._claim`. A64-015.4 §5 asked for that and it has held; a second setting appearing
beside it is what `test_one_number_still_serves_both_the_reservation_and_the_handshake` fails on.

**Recommendation.** Tune from the histogram after it has run over a weekend of real traffic.
Until then thirty seconds is a defensible assumption with a stated rationale
(`settings.py::reservation_ttl_seconds`), not a measured value, and this audit does not claim
otherwise.

---

## 3. The cooldown audit trail

**Finding: a gap, closed.**

A64-015.5 shipped the decline cooldown as one row per player, extended by `GREATEST`. Its own
documentation recorded the cost: "a second decline overwrites the first's `expires_at` and nothing
records that there were two." An operator asked "why could this player not queue at 14:30" had no
answer once the bar lifted, because the enforcement row is pruned within the hour.

`matchmaking.queue_cooldown_audit` is the record. What was built, and what each choice is for:

| Decision | Rationale |
| --- | --- |
| A **separate relation**, not a column | The join path wants a primary-key lookup; the audit path wants a player's history. Merging means either keeping A64-015.5's overwrite or turning the queue-join read into a scan-and-`max()` |
| **Append-only** | A row that can be amended answers "what does the platform say now" rather than "what happened" |
| Idempotent on `(player_id, source_match_id)` | The writer is an outbox consumer; a redelivered decline reaches it twice **by design** |
| Same transaction as the bar | A bar with no record of why is exactly what the relation exists to prevent |
| No route, no schema | Operations and support. The identifiers are internal ones a player has no use for |
| Not a `Sanction` | No actor, no severity, no note, no escalation count. A cooldown is a mechanical consequence of one action with a duration from a settings file; moderation belongs to `admin` |

**One defect was found and fixed during implementation.** `extended_existing` was first derived by
comparing the stored expiry against the requested one. That detects only the rarer case where the
*old* bar outlasted the new one; a decline thirty seconds into a sixty-second window pushes the
expiry out and leaves the two identical — which is the ordinary repeat offender, and the case a
support answer is actually about. It now reads whether a bar was in force before the write, inside
the same transaction. One indexed lookup on a path that runs once per declined match.

Evidence: `tests/unit/test_cooldown_audit.py` (18 tests) and
`tests/contract/test_matchmaking_audit.py::TestTheCooldownAuditRelation` /
`TestTheBarAndItsRecordAreOneTransaction` — including a real rollback leaving neither row, and two
concurrent sessions resolving to one row.

---

## 4. The pairing reconciliation timeline

**Finding: an event with no consumer, given one.**

`matchmaking.pairing_reconciled` has been published on every recovery since A64-015.5 and read by
nobody. The log line beside it is the wrong tool for the question three times over: it is
aggregated per tick, so it says *five tickets were settled* and not which; it sits on the log
pipeline's retention rather than the platform's; and it cannot be joined to a ticket id, which is
the only identifier a support conversation starts from.

`matchmaking.pairing_timeline` is the projection, written by a new outbox consumer
(`matchmaking_reconciliation_timeline`). It is deliberately the **cheapest consumer on the relay**:
one repository, one unit of work, one clock, and no cross-module port at all.

Two properties are worth naming because they were chosen rather than fallen into:

- **Built from the payload, never from a re-read.** By projection time the ticket may have been
  paired again or pruned, and "what did recovery do" would have changed underneath it.
- **`occurred_at` and `recorded_at` are both kept.** The gap between them is relay lag, which is
  exactly what "why was this late" is asking and is not derivable from either alone.

**Known emptiness, stated rather than hidden.** `pairing_id` is null on every row. `PairingReconciled`
identifies a *ticket*, because the reconciler claims whatever bounded batch it locks and may hold
one half of a pair without the other. The column and its partial index ship empty because §4
requires the timeline to be queryable by pairing identifier and adding a column to a populated
relation later is more expensive than shipping it null now.

Evidence: `tests/unit/test_reconciliation_timeline.py` (27 tests) and
`tests/contract/test_matchmaking_audit.py::TestTheTimelineRelation`.

---

## 5. Outbox consumer isolation

**Finding: worse than the recommendation said, now bounded.**

A64-015.5 recorded the risk as "a slow sink would delay the acceptance-failure policy". Reading
`OutboxRelay.run_once` found three problems, and the third was not in the recommendation:

1. Handlers were iterated **sequentially**, so a tick cost the *sum* of its consumers.
2. Which consumer was delayed by which was a property of a **list literal** at the composition
   root, rather than of anything anybody decided.
3. **There was no timeout at all.** A consumer that hung — not failed, hung — stopped the relay
   for that process indefinitely. Nothing in the outbox timed anything out and `OutboxSettings`
   had no timeout to set.

The third is the one that mattered, and it was about to matter more: AD-09's gateway is a network
write to a socket that may be half-open, and a consumer whose slow path is a TCP timeout sitting
in front of the acceptance-failure policy means a declined match does not requeue its opponent
until the socket gives up.

**What was changed.** `ConsumerPolicy` gives each consumer a timeout; `run_once` dispatches them
concurrently with `asyncio.gather(..., return_exceptions=True)` and wraps each in
`asyncio.wait_for`. A tick now costs the slowest consumer, and one that exceeds its budget fails
its own slice — its entries are retried, the others' work has already committed.

**What was deliberately not changed**, because §5 forbids redesigning the outbox when an
adapter-level fix suffices:

- Durability. Every entry is still claimed once, every consumer keeps its own `processed_event`
  partition, and the retry is still on the row.
- **The shared attempt budget.** `attempt_count` is per entry. A consumer that consistently fails
  an entry still spends that entry's attempts, which its healthy neighbours also draw on. Making
  it per-consumer needs a second relation and an outbox redesign, so it is **remaining debt**
  (§13) rather than a fix pretended into this task.

Concurrency is safe because the consumers share nothing: `SessionScopedNotificationHandler` opens
one session per `handle`, which A64-013.8 introduced for a different reason and which turns out to
be exactly what makes running them together correct — two handlers on one session would interleave
statements on one connection, which asyncpg does not permit.

Budgets, chosen by what each consumer does rather than by symmetry:

| Consumer | Budget | Reasoning |
| --- | --- | --- |
| `matchmaking_reconciliation_timeline` | 10s | One insert per entry, no cross-module port |
| `matchmaking_acceptance_failure` | 15s | Writes the queue; a requeue must not wait on a socket |
| `matchmaking_pending_match` | 10s | Becomes a network write when the gateway lands |
| `social_notifications` | 20s | The most collaborators |
| unregistered | 30s | A runaway guard, not a latency target |

Evidence: `tests/unit/test_outbox_isolation.py` (16 tests), including a hung consumer that the
relay returns from, its healthy neighbour completing and being recorded in the ledger, and the
timed-out entry staying claimable with `last_error = "delivery_timeout"`.

---

## 6. Metrics volume

**Finding: the existing recorder was about to become the problem.**

`LoggingMetrics` emits one structured log record per measurement. That was right for everything
that existed: every metric on the platform was per-match or per-run, so the volume was the volume
of business events. §7's pairing-scan metrics are a different kind of caller —
`MATCHMAKING_PAIRING_INTERVAL_SECONDS` is one second and `every_pool()` returns fourteen pools, so
a single naive counter there is **~1.2 million log records a day per process, on a platform with
no players on it**. The reconciler was already doing a smaller version of the same thing: its
`no_action` counter fires every five seconds, ~17,000 records a day of "nothing happened".

`AggregatingMetrics` wraps the sink. **Counters accumulate and flush as one record per series;
observations pass straight through.** The asymmetry is arithmetic, not compromise:

- A counter summed over an interval loses nothing. The sum *is* the counter, and a rate query over
  one record of 840 and over 840 records of one returns the same number.
- An observation summed over an interval loses the distribution, which is the only thing an
  observation is for — and is §2's evidence.

Memory is bounded by the **label enums, not by traffic**: every label value comes from a closed
`StrEnum`, so the number of live series is fixed at import time and is currently under forty.
That was hygiene under A64-015.5 §9 and is now load-bearing, which is why
`tests/unit/test_matchmaking_metrics.py::TestLabelsAreBounded` asserts it directly against what
was emitted rather than against the call sites.

`platform.metrics.flush` drains it on `APP_METRICS_FLUSH_INTERVAL_SECONDS` (60s). A missed flush
costs up to one interval of counters, which is the correct trade for a metric and would be the
wrong one for an event — the outbox exists for the things that must survive.

**No observability platform was introduced**, per the brief. The sink is still log records; the
day an exporter exists it replaces the sink and nothing above it changes.

---

## 7. Pairing-scan observability

**Finding: the scan was the least observable hot path on the platform, and the obvious fix was
the forbidden one.**

| Metric | Labels | Answers |
| --- | --- | --- |
| `matchmaking.pairing_scans_total` | `outcome` | Did the scan run, and what came of it |
| `matchmaking.pairing_candidates_total` | — | Mean pool depth, as `rate(candidates)/rate(scans)` |
| `matchmaking.pairing_exclusions_total` | `reason` | Why a pool with waiting players is not pairing |

`ScanOutcome` separates five endings that a single "did it pair" boolean would collapse: `paired`,
`idle`, `no_pair`, `claim_lost`, `creation_refused`. The distinction that matters operationally is
`idle` versus `no_pair` — "nobody is waiting" and "two people are waiting and neither may play the
other" are different incidents, and the second is invisible without the label.

**The cost, measured rather than asserted.** The engine compares up to n² pairs per scan.
Exclusions are counted **per excluded pair per scan**, from the two mappings the service already
holds, which is O(1) at the point they are merged — not per comparison, which at the default batch
size would be ~20,000 dictionary updates per scan and is the shape §7 forbids.
`test_doubling_the_pool_does_not_change_the_measurement_count` holds the property directly: 10 and
20 candidates produce the same number of measurements.

**What that gives up**, stated because it is a real loss: "how often did the rating window
specifically reject a pair" is not answerable. It would cost per-comparison instrumentation, and
the question is not worth that price today.

Every counter reports **even when it is zero**. A series reading zero says "the job ran and found
nothing"; an absent series says "the job did not run", and only the first lets an operator conclude
the rules are not what is holding a pool up.

Evidence: `tests/unit/test_matchmaking_metrics.py` (18 tests), including 840 idle scans through a
real accumulator reaching the sink as two records.

---

## 8. Concurrency

**Finding: no defects. No process-local locks anywhere.**

Every concurrent claim on this platform is a database lock, verified by inspection of all thirteen
`with_for_update` call sites:

| Site | Mode | Why |
| --- | --- | --- |
| `queue_repository` (3) | `SKIP LOCKED` | Two scans, two expiry sweeps or two reconcilers must divide the work, never contend |
| `cooldown_repository` | `SKIP LOCKED` | Retention's claim |
| `queue_retention_store` | `SKIP LOCKED` | Retention's claim |
| `audit_repositories` (2) | `SKIP LOCKED` | Retention's claim over the two new relations |
| `match_record_repository` — acceptance | **`FOR UPDATE`, no skip** | Correct and deliberately different: two players answering the same match must **serialize**, not skip each other. Skipping would let both reads see an unanswered match |
| `match_record_repository` — expiry sweep | `SKIP LOCKED` | Two sweepers dividing overdue matches |
| `match_retention_store` | `SKIP LOCKED` | Retention's claim |
| `outbox/repository` (3) | `SKIP LOCKED` | Two relays dividing the backlog |

`grep -rn "asyncio.Lock\|threading.Lock\|Semaphore" app/` returns nothing. The brief forbids
process-local locks for correctness and none exists — which matters because every service on this
platform is horizontally scalable and a process-local lock would be a correctness bug that only
appears on the second instance.

Three idempotency mechanisms were checked and each is a **constraint rather than a check**, which
is the only form that survives two writers:

| Mechanism | Protects |
| --- | --- |
| `uq_match__pairing_id` | One match per pairing, however many times the scan retries |
| `uq_queue_ticket__requeued_from` | One replacement ticket per requeue |
| `uq_queue_cooldown_audit__source`, `uq_pairing_timeline__event` | One audit row per fact, under concurrent delivery |

All four are asserted with **two real sessions** in the contract suite, because a fake that models
a race agrees with itself and proves nothing.

---

## 9. Retention

**Finding: complete, and now completely observable.**

Five relations, each with a stated horizon:

| Relation | Horizon | Measured on | Rationale |
| --- | --- | --- | --- |
| `queue_ticket` | 72h | `resolved_at` | "Why was I matched with them", asked the next day |
| `game.match` (abandoned) | 168h | `settled_at` | "Why did my opponent decline", asked a week later |
| `queue_cooldown` | 1h past expiry | `expires_at` | A lifted bar answers nothing |
| `queue_cooldown_audit` | 2160h (90d) | `applied_at` | The dispute arrives after the bar lifted |
| `pairing_timeline` | 336h (14d) | `occurred_at` | Bounded by the outbox horizon it projects |

**The safety property is the predicate, not the horizon.** A live ticket (`resolved_at IS NOT
NULL` excludes it) and an `active` or `pending_acceptance` match are unreachable from the deletes
however they are configured. A misconfigured window can delete too much history; it cannot delete
a player out of the queue, cannot delete the reservation recovery is about to recover, and cannot
delete a game that was played. This audit re-verified the last point specifically: no matchmaking
cleanup path can reach a completed match.

**One ordering rule was added.** `cooldown_audit_retention` must exceed `cooldown_retention`,
enforced at policy construction — an audit trail pruned before the thing it explains answers
nothing, and discovering that from an empty relation is discovering it too late.

**One observability gap was closed.** The retention metric counted two of five relations. It now
counts all five, including the ones it deleted nothing from, and
`test_the_label_enum_covers_every_relation_the_result_reports` fails if a sixth relation is pruned
without being counted.

---

## 10. Service construction

**Finding: one real defect, fixed.**

The composition root and `matchmaking.presentation.dependencies.get_metrics` each built their own
metrics recorder. That was harmless redundancy while the recorder was stateless. §6 made it
stateful — it holds counters until a flush — at which point two instances meant **counters that
nothing drained**: the request path accumulated into an object `MetricsFlushTask` never saw, and
those series simply did not exist.

`platform.metrics.process_metrics()` is now the single cached accessor both reach. It lives in
`app/platform` rather than at the composition root because `get_metrics` is a FastAPI dependency
inside a module, and a module importing the composition root inverts the layering.
`test_one_metrics_recorder_serves_the_whole_process` asserts `_metrics() is get_metrics()`.

Everything else checked out. Six services, six factories, one construction site each — asserted
per service by `TestOneFactoryPerService`, which scans the whole application tree rather than
trusting a reading. What is shared is the **factory, not the instance**: each caller gets its own
graph over its own session, because a shared service would hold a session that outlives the unit
of work it serves.

---

## 11. Architecture

**Finding: no violations. `lint-imports` reports 20 contracts kept, 0 broken.**

The boundaries were re-verified against the modules this task added, since new code is where a
boundary breaks first:

| Rule | Verified |
| --- | --- |
| `matchmaking` reaches `game` only through `game.public` | Yes — no audit module names a `game` internal |
| Domain stays framework-free | Yes — neither new domain module imports FastAPI, SQLAlchemy, pydantic or redis |
| Application holds ports, not adapters | Yes — `ReconciliationTimelineProjector` imports nothing from `infrastructure` |
| `app/platform` imports no module | Yes — including the new metrics accumulator and runtime accessor |
| The audit trail is not a product surface | Yes — no router mentions either repository or either record type |

The composition root remains the only place that names another module's concrete classes, which
is what a root is for (BR-6) and why `.importlinter`'s privacy contracts leave
`presentation/dependencies` outside their source list.

One structural improvement was made during A64-015.5 and is worth recording here because it
removed a recurring failure: `game.domain.variants` is now the home of `ProductVariant` and
`game.public.variants` re-exports it, so `domain` never imports `public` and the import cycle that
appeared twice in this epic cannot recur.

---

## 12. Test suite

**Finding: no meaningful duplication. Coverage grew where the audit found gaps.**

| Scope | Tests |
| --- | --- |
| Matchmaking and the platform surfaces it depends on | **822** |
| Whole suite | **3616 passed, 2 skipped** |
| Added by this task | 103 unit, 24 contract |

Duplication was audited by comparing every test name across the matchmaking files. Four names
repeat, and none is redundant:

| Name | Files | Verdict |
| --- | --- | --- |
| `test_an_expired_ticket_is_never_paired` | `test_pairing_engine`, `test_pairing_service` | **Keep.** The engine test asserts the *rule* over a pure function; the service test asserts the *scan*, including that the live ticket stays `waiting`. Same scenario, different subject — the pyramid working |
| `test_a_ticket_past_its_deadline_is_never_paired` | same two | **Keep**, same reasoning |
| `test_it_is_bounded` | three files | **Keep.** Generic name, three different bounded reads |
| `test_it_has_its_own_ledger_partition` | two consumer suites | **Keep.** The same property asserted of two different consumers, and it must hold for each |

The unit/contract split was checked rather than assumed. Every property that belongs to PostgreSQL
— partial-index predicates, `ON CONFLICT`, `SKIP LOCKED`, rollback atomicity — is asserted in
`tests/contract/` with real sessions, and the in-memory fakes document in their own docstrings
which behaviour they model and which they decline to. `tests/fakes/audit.py` follows that
convention: it models the unique index because the duplicate path is the one a redelivery takes,
and explicitly does not model the atomicity.

---

## 13. Remaining technical debt

Ordered by what would be worth doing first.

| # | Debt | Cost today | What it needs |
| --- | --- | --- | --- |
| 1 | The acceptance deadline is unvalidated (§2) | A window that may be too short for real players, or needlessly long | A weekend of histogram data. No code |
| 2 | The outbox attempt budget is per entry, not per consumer (§5) | A poison consumer can exhaust a shared budget on one entry | A second relation and an outbox change — a task, not a fix |
| 3 | No realtime gateway (§6) | Clients poll. Specified, tested, and slower than a socket | AD-09 |
| 4 | No metrics exporter (§7) | Counters are queryable only through the log pipeline | A dependency decision, which is outside a task's authority |
| 5 | Per-comparison exclusion detail is not counted (§7) | "How often did the rating window reject a pair" is unanswerable | Per-comparison instrumentation, deliberately declined |
| 6 | `pairing_timeline.pairing_id` is always null (§4) | The by-pairing query returns nothing | `PairingReconciled` carrying a pairing, which it structurally may not today |
| 7 | `every_pool()` scans all fourteen pools regardless of occupancy | Fourteen queries a second on an empty platform | A pools-with-waiting-tickets query, worth it when the pool count grows |

---

## 14. Before live game integration

What a task building on this should know, in the order it will matter:

1. **The handshake is complete and recoverable.** Pair, create, accept, decline, expire, requeue,
   reconcile — every branch has a defined outcome and a test. Nothing here is a stub.
2. **`game.public` is the whole surface.** Four use cases and a bundle of stateless engine
   collaborators. Do not reach past it; `lint-imports` will refuse, and the reason is R-1 rather
   than tidiness.
3. **Everything is at-least-once.** Any consumer added to the relay must be idempotent, must
   return per-entry failures rather than raising, and must open its own session. A consumer that
   does the first two gets isolation for free; one that does neither takes its neighbours down
   with it.
4. **The deadline is the one number to watch.** It is instrumented and unvalidated. Read the
   histogram before assuming thirty seconds is right, and before assuming it is wrong.
5. **The audit trails are for operators.** No route reaches them and none should. A player-facing
   history is a separate product decision, not an exposure of these relations.
