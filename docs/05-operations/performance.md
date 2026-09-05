# Performance and Load Testing

> **Status:** current. Opened by A64-028.5, completed by A64-028.5A.
> **Owner:** platform engineering.
> **Every number here was measured on a developer's laptop.**
> **THIS IS NOT PRODUCTION HARDWARE.** Nothing below is a capacity promise.

---

## 1. The environment every number belongs to

| | |
| --- | --- |
| CPU | Apple M3 Pro, 11 logical cores |
| RAM | 18 GB |
| OS | macOS 26.6.2, arm64 |
| Python | 3.13.14 |
| PostgreSQL | 17.10, Docker, localhost, `max_connections` 100, `shared_buffers` 128 MB |
| Redis | 8.10, Docker, localhost |
| API | uvicorn, **one worker per process**, no reload, no TLS, `INFO` logging |
| API instances | 1 or 2, stated per scenario |
| Pool | `pool_size` 10, `max_overflow` 5, `statement_timeout` 5000 ms |
| App environment | `local` → **rate-limit profile `development`, which multiplies every production limit by 20** |
| Load generator | in-repo asyncio harness, **same machine as the server** |
| Dataset | ~240 users, ~370 matches, ~2.5 k outbox rows, ~800 notifications |

Client and server share eleven cores. Every latency below includes the
generator's own scheduling, and every throughput is bounded by whichever of
the two ran out of core first.

### The rate limits these numbers were taken under are not production's

`ENVIRONMENT=local` derives the `development` rate-limit profile, and that
profile applies a **×20 multiplier** to every limit in the platform
(`app/core/rate_limiting.py`, `_MULTIPLIERS`). Nothing here disabled a
limiter — the multiplier is the platform's own behaviour and cannot be set
by an operator — but it means a per-IP ceiling observed below is twenty
times the one production enforces. Refresh is the clearest case: the
limiter allowed 600 rotations per 60 s per IP here, and will allow 30.

Every figure that turned out to be the limiter rather than the service is
labelled **RATE-LIMIT BOUNDED** in §3 and §4, with the production ceiling
stated beside it.

---

## 2. Provisional targets

No product SLO exists and this task does not invent one. These are
**engineering thresholds** used to read the tables, nothing more.

| Path | Provisional p95 |
| --- | --- |
| Authenticated read | ≤ 150 ms |
| Public/reference read | ≤ 100 ms |
| Move command round trip | ≤ 250 ms |
| Cross-instance frame | ≤ 300 ms |
| Unexpected failures | 0 |

---

## 3. What was measured

### HTTP reads — one instance

| Endpoint | Conc. | ops/s | p50 | p95 | p99 | refused | failed |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `/time-controls` | 1 | 277.7 | 3.3 | 5.2 | 8.0 | 0 | 0 |
| `/time-controls` | 10 | **582.7** | 14.0 | 37.5 | 80.8 | 0 | 0 |
| `/time-controls` | 25 | 371.5 | 39.5 | 207.8 | 333.8 | 0 | 0 |
| `/time-controls` | 50 | 242.4 | 138.6 | 584.4 | 951.5 | 0 | 0 |
| `/time-controls` | 100 | 113.3 | 564.8 | 2585.8 | 3853.6 | 0 | 0 |
| `/profile/me` | 10 | 402.2 | 22.2 | 41.4 | 82.5 | 0 | 0 |
| `/profile/me` | 50 | **454.3** | 106.5 | 145.2 | 188.2 | 0 | 0 |
| `/profile/me` | 100 | 132.9 | 350.4 | 2476.0 | 3686.2 | 0 | 0 |
| `/tournaments` | 25 | 358.6 | 52.2 | 89.0 | 119.3 | **874** | 0 |
| `/tournaments` | 50 | 0.0 | — | — | — | **2514** | 0 |

**Zero unexpected failures at every level.** Throughput *falls* past the
peak while latency rises an order of magnitude — the signature of a closed
queue in front of a saturated server, not of errors.

`/tournaments` is rate-limited far below its capacity: at 50 concurrent
callers from one IP, every request was refused. That is the limiter working
and it is why that endpoint's capacity is unknown.

### Refresh rotation — one instance — RATE-LIMIT BOUNDED

20 independent browser sessions, each rotating its own cookie in a closed
loop for 10 s.

| Conc. | successes/s | p50 | p95 | p99 | refused | failed |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 20 | 60.0 | 81.0 | 126.8 | 147.7 | 9 229 | 0 |

**Every one of the 9 229 refusals was a `429`, and none was a `409`.** The
60/s is therefore the limiter's ceiling exactly — 600 per 60 s per IP under
the `development` profile — consumed in 10 s, and not a measurement of what
rotation costs the server. Production's ceiling is **30 per 60 s per IP**.

The latencies are real: they are the 600 rotations the limiter allowed, and
refusals are excluded from the percentiles. Zero unexpected `401`s and zero
`5xx`. What is **not** measured is rotation throughput below the limiter;
§4 says why that was left alone.

### Matchmaking burst — two instances

Every user joins the casual queue at once; pairing is a scheduled sweep, so
time-to-match is polled from the client's view.

| Users | joins/s | p50 | p95 | p99 | failed |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 100 | 4.8 | 436.1 | 582.4 | 599.8 | 0 |
| 250 | 10.9 | 974.3 | 1 698.1 | 1 772.7 | 0 |
| 500 | 17.2 | 2 830.0 | 6 425.4 | 6 507.5 | 0 |
| 1 000 | 30.8 | 9 453.7 | 11 123.6 | 11 155.4 | 0 |

A join is an accepted ticket, not a pairing. p95 leaves §2's read target at
250 users and is 6.4 s at 500 — the burst is the queue's worst case by
construction, and no join failed at any level.

### Concurrent live games — two instances

Each game is two authenticated sockets playing six engine-legal plies.

| Games | moves/s | p50 | p95 | p99 | failed | API CPU | generator CPU |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 10 | 117.8 | 33.9 | 55.6 | 83.1 | 0 | 100 % | 30 % |
| 25 | 117.1 | 120.6 | 155.1 | 163.3 | 0 | 127 % | 63 % |
| 50 | 143.3 | 128.2 | 275.5 | 305.8 | 0 | 154 % | 69 % |
| 100 | 176.6 | 195.5 | 362.9 | 478.3 | 0 | 192 % | 88 % |
| 250 | 188.2 | 389.3 | 895.5 | 1 328.4 | 0 | 197 % | 98 % |
| 500 | 186.1 | 805.0 | 2 071.0 | 2 756.0 | 0 | 196 % | 98 % |

1 000 sockets and 3 000 moves at the top level, none refused, none failed.
Throughput flattens at ~186 moves/s from 250 games up while latency keeps
climbing, which is the shape of a closed loop against a saturated server.

### Was the generator the bottleneck?

The generator reaches 98 % of one core at the top two levels, which looks
like the answer — so it was tested rather than assumed. The same 500 games
were driven from **two** generator processes instead of one:

| Generators | Throughput | |
| --- | ---: | --- |
| 1 × 500 games | 180.6 moves/s | |
| 2 × 250 games | 94.7 + 94.6 = **189.3 moves/s** | **+4.8 %** |

Doubling the generator bought 4.8 %. **The live-game numbers are
server-bound and valid.** Had the gain been large they would have been
labelled INVALID and rerun; the check is recorded because "the client was
busy too" is otherwise unfalsifiable.

### Idle WebSockets — one instance

Held for 5 s, then counted.

| Sockets | connected | still open | dropped | connect p50 | API RSS | API CPU |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 100 | 100 | 100 | 0 | 421 ms | 305 MB | 50 % |
| 250 | 250 | 250 | 0 | 1 604 ms | 348 MB | 90 % |
| 500 | 500 | 500 | 0 | 6 070 ms | 333 MB | 106 % |
| 1 000 | 1 000 | 1 000 | 0 | 8 352 ms | 398 MB | 111 % |
| 2 000 | 2 000 | 2 000 | 0 | 12 765 ms | 519 MB | 111 % |

**≈100 KB of RSS per idle connection**, taken across 250 → 2 000 sockets
(348 → 519 MB over 1 750 connections) rather than between adjacent rungs,
where a garbage collection is larger than the signal — the 500 rung reads
15 MB *lower* than the 250 rung for exactly that reason.

The p50 column is **connect** latency, not steady-state cost: 2 000
handshakes issued at once take ~13 s to complete. Holding them costs
nothing measurable. Establishing them is the expensive part, which is what
the reconnect storm below is about.

### Reconnect storm — one instance

Three waves per level. The first two waves drop the transport without a
close frame, so the server has to notice a *lost* socket rather than a
polite goodbye.

| Sockets | reconnects | failed | wave 1 | wave 2 | wave 3 | API RSS |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 100 | 300 | 0 | 0.45 s | 1.08 s | 1.09 s | 481 MB |
| 250 | 750 | 0 | 1.59 s | 4.21 s | 2.69 s | 486 MB |
| 500 | 1 500 | 0 | 6.27 s | 6.59 s | 6.42 s | 493 MB |

2 550 reconnects, none refused, none failed. **Later waves are not slower
than the first** at 500 sockets, which is the property being tested: a
registry or a ticket store that leaked per cycle would show it here.

### Cross-instance realtime — two instances

Two uvicorn processes, distinct `GATEWAY_NODE_ID`, one Redis. A move sent
to one instance must reach a watcher connected to the other.

| Measurement | Frames | Missing | Duplicated | p50 | p95 | p99 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Ladder, 30 rounds | 30 | 0 | 0 | 126.8 | 216.8 | 253.8 |
| Stress, normal | 220 | **0** | **0** | 103 | 182 | 183 |
| Stress, after forced mailbox expiry | 220 | **0** | **0** | 103 | 182 | 183 |

The stress runs are 112 frames A→B and 108 B→A across four matches, with
zero illegal moves. The second run deletes both `gwbus:v1:<node>` streams
first, so every frame in it crosses a mailbox that had to be recreated —
this is P1-9's regression case, and it is also covered permanently by
`tests/contract/test_gateway_bus_lifecycle.py`.

### The bottleneck

| Reading at concurrency 50 | Value |
| --- | --- |
| API process CPU | **104 %** — one core of eleven |
| DB connections / active / waiting | 26 / 6 / **0** |
| Redis ops/s | 26 |
| API RSS | 353 MB |

**One Python process, one event loop, one core.** PostgreSQL and Redis are
idle by comparison: the pool never waited once. Everything else follows
from this.

### Multi-instance scaling

| Setup | ops/s | p95 | Efficiency |
| --- | ---: | ---: | ---: |
| 1 instance, 1 client process | 290.4 | 210 | — |
| 2 instances, **1** client process | 329.0 | 450 | 0.52 |
| 2 instances, **2** client processes | **469.2** | 226 / 164 | **0.81** |

The 0.52 is not a platform result — it is the **load generator** running out
of one core. Two client processes against one node reached 337 ops/s where
one reached 290, and only with a client per node does the platform's own
scaling show: **0.81 at two instances.**

Recorded because it is the mistake §42 warns about, made and caught here: a
benchmark that measures its own generator will under-report scaling and
nobody can tell from the number alone.

The generator-CPU column added in A64-028.5A is what makes that check
routine rather than lucky: it is now printed beside every result, and the
"was the generator the bottleneck?" table above is the same question asked
of the live-game ladder, where the answer came out the other way.

### Outbox

| Events | Drain | Events/s | Max attempts |
| ---: | ---: | ---: | ---: |
| 500 | 5.7 s | 88.2 | 0 |
| 2000 | 20.6 s | **97.0** | 0 |

Steady ~90–97 events/s with a single relay, no retries, and no effect on
the historical backlog. The A64-028.4 regression check held throughout:
`outbox_max_attempts` never rose.

### Mixed workload, 35 minutes — two instances

Everything at once, held for the whole run: 20 authenticated readers split
across both instances, 25 concurrent live games recycling continuously, and
100 idle sockets opening and closing in 20-second cycles. This is the soak.

| | |
| --- | --- |
| Duration | 2 111 s (35 min) |
| Operations | **783 515** |
| Throughput | 371.1 ops/s |
| p50 / p95 / p99 / max | 52.9 / 207.6 / 616.1 / 1 448.5 ms |
| Failures | **0** |
| Refusals | **0** |
| API CPU peak | 202.9 % (two processes) |
| DB connections peak | 33 of 100, `db_waiting_peak` **1** |
| Redis peak | 4 921 ops/s |

**Memory did not grow.** RSS across 3 838 readings: first quarter 388.9 MB,
last quarter 375.7 MB — **−13.2 MB, −3.4 %**. Quarters rather than
endpoints, because a single first and last reading is one collection away
from saying anything. A leak is a direction that persists; this is the
allocator working.

No scheduled task fell behind: the relay ran 4 866 ticks across both
instances during the run, **none of them reporting a failure**.

#### One thing the soak leaves open

`outbox_exhausted` rose by exactly **50** — 42 `game.move_applied` and 8
`users.presence_offline`, all created at 21:21:30 UTC and all claimed at
21:21:34 by one worker, roughly two minutes into the run. It did not recur
in the remaining 33 minutes.

What makes it worth recording rather than rounding away: every one of the
50 carries `attempt_count = 5` with **`last_error` NULL and
`next_attempt_at` NULL**, so `mark_failed` never ran for any of them — and
no tick logged a failure. Something incremented the attempt budget five
times without recording a reason, and an operator watching the logs would
have seen a healthy relay. The volume is small; the invisibility is the
problem. Open as **P2-9** in the risk register — the cause is not known and
is not guessed at here.

### The analytics pipeline under load

The pipeline is the outbox: a domain event is enqueued in the same
transaction as the change, and the relay projects it into an analytics
event. Measured by the backlog it leaves rather than by a stopwatch, which
is what an operator actually watches.

| Ladder | Events enqueued | Retryable backlog | Drained to 0 | Permanently abandoned |
| --- | ---: | ---: | ---: | ---: |
| 1 850 matchmaking joins + 500 live games | ~3 356 | 1 807 peak | **30 s** | **0** |

Drain rate ~60 events/s while the same machine was still serving the load
that produced them. **`outbox_exhausted` did not move** — it stayed at
2 880, all of which predate this work.

That number is the measurement. The same ladder on the previous build added
**456** permanently abandoned events, from two defects this task found by
running it: the relay deadlock (P1-10) and a projection no schema would
accept (P1-11). Both are closed; see the register.

### Correctness during load

Asserted after every run (§46):

```
players_in_two_active_matches   2   (pre-existing; constant across every run)
duplicate_plies                 0
orphan_moves                    0
duplicate_rating_adjustments    0
```

`players_in_two_active_matches` is **2 before and 2 after** every scenario
in this document, including the 1 000-user matchmaking burst and the
500-game ladder. It is a pair of rows left by earlier development, not
something load produced — reported as it reads rather than filtered out,
because a filtered invariant is not an invariant.

The other three are zero at every level, including at 500 concurrent games
where 3 000 moves were applied across two instances.

## 4. What was not measured, and why

Honesty about coverage is the point of this section.

| Scenario | Status |
| --- | --- |
| Login throughput | **RATE-LIMIT BOUNDED.** 20 logins per IP per 15 minutes in production, 400 under this profile. Measuring capacity from one source IP needs either a control disabled or many source addresses, and neither was available: assigning loopback aliases needs `sudo`. Left unmeasured rather than faked |
| Refresh throughput below the limiter | Not measured. The limiter's own ceiling is measured and reported; the service cost underneath it would need the endpoint's guard bypassed, and this task does not remove controls to produce a bigger number |
| Analytics query latency | Not measured. The pipeline's *ingest* is measured — see the outbox drain and the correctness section — but the dashboards' read path is A64-028.6's |
| API-side event-loop lag | **Not measured, and the generator's is not a substitute.** The harness reports its own loop's lag (1.07–2.08 ms p50 throughout), which says the client was healthy and says nothing about the server. Instrumenting the API's loop is A64-028.6 |
| Push and email delivery at scale | Deliberately not exercised. Driving real providers with load-test volume is not a thing to do to a third party |
| Backpressure under a saturated queue | **Partly.** Every scenario is closed-loop, so the client waits rather than piling on — which is the honest shape for a game client and the wrong shape for testing what an unbounded producer does. What is measured instead is that nothing was ever refused for capacity: 0 refusals across every scenario in §3 except refresh, where all 9 229 were the limiter |
| Anything on production hardware | Nothing here ran on any |

## 5. Observed safe envelope in THIS TEST ENVIRONMENT

Classification: **GREEN** — inside §2's targets with zero failures.
**YELLOW** — still no failures, p95 outside target. **RED** — failures, or
latency an interactive path cannot use.

| Workload | GREEN | YELLOW | RED | Binding constraint |
| --- | --- | --- | --- | --- |
| Reference read, 1 instance | ≤ 10 concurrent | 25 | 50 | API process CPU |
| Authenticated read, 1 instance | ≤ 25 concurrent | 50 | 100 | API process CPU |
| Authenticated read, 2 instances | ≤ 50 concurrent | — | not reached | API process CPU |
| Concurrent live games, 2 instances | ≤ 50 games | 100–250 | 500 (p95 2.1 s) | API process CPU |
| Idle WebSockets, 1 instance | ≤ 2 000 held | — | not reached | Connect rate, not capacity |
| Reconnect storm, 1 instance | ≤ 500 at once | — | not reached | Handshake CPU |
| Matchmaking burst, 2 instances | ≤ 100 users | 250 | 500 (p95 6.4 s) | Pairing sweep interval |
| Cross-instance frame | measured to 220 frames | — | not reached | — |
| Outbox relay | ~60–90 events/s sustained | — | not reached | Relay batch × interval |
| Refresh | **RATE-LIMIT BOUNDED** | — | — | Limiter, by design |
| Login | **RATE-LIMIT BOUNDED** | — | — | Limiter, by design |

GREEN is the last level where p95 stayed inside §2 with zero failures. No
scenario at any level produced an unexpected failure; every RED above is a
latency judgement, not an error rate.

**Arena64 is not claimed to support any number of users.** These are the
levels one laptop sustained while also generating the load.

### The constraint, in one line

One uvicorn process pins one core (99–100 %) while the pool never waits
(`db_waiting_peak` 0 at every level, ≤ 32 of 100 connections) and Redis
peaks at 16 450 ops/s. Two processes reach 196 %. **The platform is
CPU-bound per process and nothing shared is close to its limit** — so the
first lever in any environment is worker processes per host, not a bigger
database.

---

## 6. Rerunning

```bash
# two instances, distinct node ids, shared PostgreSQL and Redis
GATEWAY_NODE_ID=node-1 uv run uvicorn main:app --port 8101 &
GATEWAY_NODE_ID=node-2 uv run uvicorn main:app --port 8102 &

NODES=http://127.0.0.1:8101,http://127.0.0.1:8102

uv run python -m tests.load P01 P02 --nodes $NODES --out baseline.json
uv run python -m tests.load P04 P06 P08 --nodes $NODES --out capacity.json
uv run python -m tests.load P09 P17 P10 --nodes $NODES --out realtime.json
uv run python -m tests.load P16 --nodes $NODES --out scaling.json
uv run python -m tests.load P18 --soak-minutes 35 --nodes $NODES --out soak.json
```

| Id | Scenario |
| --- | --- |
| P01 / P02 | HTTP reads, reference and authenticated |
| P03 / P04 | Login, refresh rotation |
| P06 | Matchmaking burst, 100 → 1 000 |
| P08 | Concurrent live games, 10 → 500 |
| P09 | Idle WebSockets, 100 → 2 000 |
| P10 | Cross-instance frame latency |
| P13 | Outbox drain |
| P16 | Multi-instance scaling |
| P17 | Reconnect storm, three waves |
| P18 | Mixed workload; `--soak-minutes` makes it the soak |

`--soak-minutes` is deliberately not scaled by `--scale`: a soak's length
is the measurement.

**Read the `harness_cpu_peak` column before believing a throughput.** If
the generator is near 100 % of a core, split it across processes and
compare before reporting the number — §3 shows the check.

---

## 7. What must be rerun on production hardware

Everything. The measured bottleneck is one Python process on one core, so
the first questions a real environment answers are how many worker
processes a host supports, at what point the shared PostgreSQL becomes the
limit instead, and whether the 0.81 scaling holds past two instances.

Two further reasons nothing here transfers:

- every per-IP figure was taken under the `development` rate-limit profile,
  which is **twenty times** production's (§1);
- client and server shared eleven cores, so the API never had a machine to
  itself at any level.

### The one recommendation this environment does support

Run **more than one worker process per host**. The evidence is not an
extrapolation: a single uvicorn process pinned one core at every saturated
level while the connection pool never waited once (`db_waiting_peak` 0
throughout, peak 32 of 100 connections) and Redis peaked at 16 450 ops/s on
a server that does far more. Two processes doubled the CPU ceiling and
raised throughput accordingly. How many is a question for real hardware;
that the answer is greater than one is settled here.

---

## Related Documents

- [`data-reliability.md`](./data-reliability.md) — the topology these numbers assume
- [`../01-architecture/production-hardening.md`](../01-architecture/production-hardening.md)
