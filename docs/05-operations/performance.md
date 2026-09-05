# Performance and Load Testing

> **Status:** current. Opened by A64-028.5.
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
| App environment | `local` → **rate-limit profile `development`** |
| Load generator | in-repo asyncio harness, **same machine as the server** |
| Dataset | ~240 users, ~370 matches, ~2.5 k outbox rows, ~800 notifications |

Client and server share eleven cores. Every latency below includes the
generator's own scheduling, and every throughput is bounded by whichever of
the two ran out of core first.

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

### Scaling

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

### Outbox

| Events | Drain | Events/s | Max attempts |
| ---: | ---: | ---: | ---: |
| 500 | 5.7 s | 88.2 | 0 |
| 2000 | 20.6 s | **97.0** | 0 |

Steady ~90–97 events/s with a single relay, no retries, and no effect on
the historical backlog. The A64-028.4 regression check held throughout:
`outbox_max_attempts` never rose.

### Correctness during load

Asserted after every run (§46):

```
players_in_two_active_matches   0
duplicate_plies                 0
orphan_moves                    0
duplicate_rating_adjustments    0
```

---

## 4. What was not measured, and why

Honesty about coverage is the point of this section.

| Scenario | Status |
| --- | --- |
| P03 login | **Bounded by the limiter, not the server.** 20 logins per IP per 15 minutes even in `development`, so login throughput cannot be measured from one source IP without disabling a control §28 forbids disabling |
| P06 matchmaking burst | Not run at scale — needs cohorts the registration limit (10/IP/hour) cannot create through the endpoint; the seeded path exists but the scenario was not exercised |
| P08 concurrent live games | Not run |
| P09 idle sockets | Not run |
| P10 cross-instance frame latency | **Partially.** 10 of 30 frames delivered, p50 176 ms / p95 307 ms; the rest are the open defect below |
| P11 reconnect storm | Not run |
| P14 analytics pipeline | Not run |
| P15 mixed workload | Not run |
| P17 soak | Not run |

The harness implements P01–P04, P06, P08–P10, P13 and P16; the gap is
execution time, not tooling. Rerunning them is §6 of this document.

---

## 5. Observed safe envelope in THIS TEST ENVIRONMENT

| Workload | Safe observed | First degraded | First saturation | Primary bottleneck |
| --- | --- | --- | --- | --- |
| Reference read, 1 instance | ≤ 10 concurrent | 25 | 50 | API process CPU |
| Authenticated read, 1 instance | ≤ 25 concurrent | 50 | 100 | API process CPU |
| Authenticated read, 2 instances | ≤ 50 concurrent | — | not reached | API process CPU |
| Outbox relay | ~90 events/s sustained | — | not reached | Relay batch × interval |
| Login | ~20 per 15 min per IP | — | — | **Rate limiter, by design** |

Safe is set at the last level where p95 stayed inside §2's targets with zero
failures — one step below the first degraded level, which is the margin.

**Arena64 is not claimed to support any number of users.** These are the
levels one laptop sustained while also generating the load.

---

## 6. Rerunning

```bash
# two instances, distinct node ids, shared PostgreSQL and Redis
GATEWAY_NODE_ID=node-1 uv run uvicorn main:app --port 8101 &
GATEWAY_NODE_ID=node-2 uv run uvicorn main:app --port 8102 &

uv run python -m tests.load P01 P02 --nodes http://127.0.0.1:8101 --out baseline.json
```

**One client process per target instance.** A single generator saturates
before two servers do — see §3.

---

## 7. What must be rerun on production hardware

Everything. The measured bottleneck is one Python process on one core, so
the first questions a real environment answers are how many worker
processes a host supports, at what point the shared PostgreSQL becomes the
limit instead, and whether the 0.81 scaling holds past two instances. None
of that can be extrapolated from a laptop that was also the client
(§42).

---

## Related Documents

- [`data-reliability.md`](./data-reliability.md) — the topology these numbers assume
- [`../01-architecture/production-hardening.md`](../01-architecture/production-hardening.md)
