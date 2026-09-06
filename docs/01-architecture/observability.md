# Observability

> **Status:** current. Opened by A64-028.6.
> **Owner:** platform engineering.
> **Closes:** A64-028.1's **P1-4** — "nothing can be observed in production".

---

## 1. What was actually missing

The finding was never that the platform was uninstrumented. It emitted 41
metrics at the right instants with bounded labels, and
`app/platform/metrics/__init__.py` had said since A64-015 that the recorder
was a seam an exporter would plug into. What was missing was the way out of
the process: no `/metrics`, no storage, no dashboard, no alert.

The audit that opened this task found the sharper version. Of those 41
metrics, **not one was about an HTTP request** — every pairing scan was
counted and whether the API answered was not — and `configure_logging` pins
`uvicorn.access` to `WARNING`, so there was no request log either.

| | Before | After |
| --- | --- | --- |
| Metric names | 41 | 60 |
| HTTP metrics | 0 | 4 |
| Outbox metrics | 0 | 9 |
| Rate-limiter metrics | 0 | 3 |
| Gauges | 0 — the port had none | 6 |
| Exporter | none | `/metrics`, token-guarded |
| Dashboards | none | 4, version controlled |
| Alert rules | none | 18, each with a runbook |
| Log redaction | none | at the handler |

---

## 2. How a measurement leaves the process

```
call site  →  process_metrics()  →  AggregatingMetrics  →  FanOutMetrics ┬→ LoggingMetrics → stdout
                                                                          └→ PrometheusMetrics → /metrics
```

Nothing at the call sites changed. `FanOutMetrics` feeds both sinks because
they are for different readers: a log is what an incident is reconstructed
from afterwards, a series is what an alert fires on at the time.

**Counters arrive in steps.** They pass through `AggregatingMetrics`, which
flushes on a schedule, so a counter's total is exact but moves at the flush
interval. Observations bypass aggregation, so latency histograms are live.
Set `APP_METRICS_FLUSH_INTERVAL_SECONDS` at or below the scrape interval —
the production compose sets both to **15** — or `rate()` reads the steps
rather than the traffic.

### Gauges, and why they arrive differently

`MetricsRecorder` has `increment` and `observe` and deliberately no gauge.
The reason `ports.py` gives is exact: "a gauge is a value read at scrape
time, which needs the exporter to call *into* the application — the one
shape that cannot be expressed as a log line."

That was true while the only sink was a log. It is not any more, so gauges
arrive the way that reasoning implies: a `GaugeSource` is registered with
the exporter and called **during the scrape**.

| Gauge | Read from |
| --- | --- |
| `arena64_http_requests_in_flight` | a counter the middleware holds |
| `arena64_service_draining` | the process's drain flag |
| `arena64_outbox_backlog{state}` | the relay's last tick |
| `arena64_outbox_oldest_pending_age_seconds` | the relay's last tick |
| `arena64_backup_last_success_timestamp_seconds` | a JSON file in the backup destination |
| `arena64_certificate_expiry_timestamp_seconds` | the TLS certificate on disk |

The outbox gauges are refreshed by the relay's own tick from the session it
already has open, not by the scrape. A gauge source that opened a database
session per series would make the monitoring into the load, and one poll
interval of staleness is finer than any scrape interval worth configuring.

---

## 3. Naming

`gateway.moves_rejected_total` becomes `arena64_gateway_moves_rejected_total`:
dots to underscores, `arena64_` prefix. The mapping is one function
(`prometheus_name`) and is total, so it cannot drift from a lookup table
nobody updates.

A metric that arrives with labels other than the ones its series was
declared with is **dropped and counted** in
`arena64_metrics_dropped_total{reason}`, rather than raising inside a
request. A silently dropped measurement is the one failure an operator would
otherwise have to infer from a gap.

---

## 4. Cardinality

**No high-cardinality label is emitted anywhere.** Every label value is a
`StrEnum` member or a `Final` constant. There is no `user_id`, `match_id`,
`tournament_id`, `email`, IP, session id, exception message, or raw URL in
any label, and `tests/unit/test_matchmaking_metrics.py` asserts it for the
subsystem that has the most.

Two places where it would have been easy and was refused:

- **The route label.** `/api/v1/matches/{match_id}`, never
  `request.url.path`. A request matching no route is labelled `unmatched`
  rather than by its path — otherwise a 404 sweep from a scanner mints one
  series per probed URL, which is a denial of service against the metrics
  backend that anyone on the internet can perform.
- **The outbox failure reason.** A closed four-member enum, never
  `type(error).__name__`. A driver can invent an exception name; a label a
  dependency controls is a cardinality bomb pointed at the backend.

Estimated live series: roughly 200, fixed at import. Nothing grows with
traffic.

---

## 5. Getting the metrics out

`GET /metrics`, unversioned, `Authorization: Bearer <OPS_TOKEN>`, compared
in constant time.

**Two boundaries, and neither is enough alone.** The route refuses a caller
without the token; the edge refuses `/metrics` from the public internet
entirely (`infrastructure/production/Caddyfile`). Either alone has a way of
being wrong — a mislaid token, a misconfigured proxy — and the combination
is what makes both survivable.

A production-like tier with the operator surface enabled and neither a token
nor `OPS_ALLOW_UNAUTHENTICATED=true` **refuses to start**. A deployment that
scrapes over a private network with no token is a legitimate choice; a
deployment that meant to set a token and did not is a leak, and the two are
indistinguishable from configuration alone. The operator says which.

`/metrics` and `POST /health/drain` share one token: same audience, same
blast radius, and two secrets an operator has to keep in step is how one of
them ends up unset.

---

## 6. Event-loop lag

The failure that makes an asyncio service stop answering while every
dependency it has is healthy. A64-028.5A measured the **load generator's**
loop and said so; that number is honest and useless for operating the
platform.

`EventLoopLagProbe` sleeps for a fixed interval and records how much later
than that it woke. The loop can only return late, and it returns late by
exactly as long as it was busy elsewhere, so the drift is the lag.
`perf_counter`, because a clock adjustment mid-measurement would otherwise
read as a stall. One coroutine, one wake-up a second, one subtraction.

Measured on this task's two-instance local deployment:

| | samples | p50 | p95 | p99 | mean |
| --- | ---: | ---: | ---: | ---: | ---: |
| Idle | 50 | ≤ 5 ms | ≤ 5 ms | ≤ 25 ms | 1.9 ms |
| 500 concurrent live games | 56 | ≤ 5 ms | ≤ 5 ms | ≤ 250 ms | 4.2 ms |

Quantiles are bucket-bounded, so "≤ 5 ms" means the bucket rather than the
value. What the p99 says is that at least one sample under load exceeded
50 ms — the CPU saturation A64-028.5A measured, showing up as scheduling
drift, which is what this metric exists for.

**This is a developer's laptop, running the client and both servers.** It is
not a production capacity claim and nothing here should be read as one.

---

## 7. Dashboards

`infrastructure/observability/dashboards/`, provisioned from
`grafana-provisioning.yml`. Four, matching the four questions:

| Dashboard | Answers |
| --- | --- |
| `api-health` | is it serving, failing, slow, is the loop turning |
| `realtime` | sockets, cross-instance delivery, moves, matchmaking |
| `data-pipeline` | outbox, rate limiter, analytics, backup age |
| `external-delivery` | email and push, by outcome |

`allowUiUpdates: false`. An operator who edits a panel in Grafana is making
a change the next deploy discards, which is correct: a panel worth keeping
is worth a commit.

---

## 8. Alerts

`infrastructure/observability/alerts.yml`, 18 rules, validated by
`promtool check rules`. Each names a failure mode this platform has had or
that A64-028.1 identified, says what an operator does, and links a runbook
section.

Thresholds are marked **MEASURED** where A64-028.5A produced the number and
**PROPOSED OPERATIONAL DEFAULT** where it is chosen. None is a product SLO.

`apps/api/tests/unit/test_observability_config.py` refuses a rule or a panel
that queries a series nothing emits, and a runbook link that does not
resolve. Both failures are invisible in Grafana, in Prometheus and in
review: a panel on a missing metric draws a flat line, and an alert on a
misspelled one never fires — which is exactly what a working alert looks
like until the day it was needed.

---

## 8a. The edge's own logs — A64-028.6A

Nginx is not instrumented for Prometheus. That is a documented defer rather
than an omission: an exporter is another container, another scrape target
and another thing to keep in step, and everything an alert would fire on is
already measured **behind** it — request rate, status classes and latency by
`app/api/http_metrics.py`, upstream failure as a 5xx, and instance
availability as Prometheus's own `up`.

What the edge adds that the application cannot see is in its access log,
which is JSON and carries `upstream`, `upstream_status`, `upstream_time` and
the negotiated `protocol`. That is what answers "which replica served this"
and "did nginx retry", and it is the first thing the upstream runbook asks
for.

**Open:** a request that never reaches an upstream — a TLS handshake
failure, a client the edge rejected — is in the log and in no metric. If
that becomes a question worth alerting on, `nginx-prometheus-exporter` is
the minimal answer, and it must not publish on a public port.

---

## 9. Logs

One JSON object per line to stdout, with `request_id`, `correlation_id` and
`causation_id` on every record.

**Redaction is at the handler**, which `CLAUDE.md` §8.3 requires and
A64-028.1 found at neither the handler nor the call sites. It matches by
field **name**, not by sniffing values: a value sniffer is a regex arms race
that fails open on the first credential shaped differently from the pattern,
while a name list fails closed on the field a call site actually passed.

Redacted: `authorization`, `cookie`, any `token`, `password`, `secret`,
`api_key`, `otp`, `vapid`, `dsn`, `email`, `body`, `payload`.
Kept: the domain identifiers an incident is reconstructed from, and
`token_family` explicitly — it is what A64-028.2's reuse detection is read
from and it is not a credential.

The line this draws is the platform's existing one, stated in
`platform/metrics/__init__.py`: identifiers are kept out of **metrics** and
allowed in **logs**.

---

## 10. What is still not observed

Stated rather than implied.

| Gap | Why, and who owns it |
| --- | --- |
| Host CPU, memory, disk | No node exporter. The application reports its own RSS and nothing about the machine it is on — so [disk pressure](./runbooks.md#disk-pressure) has a runbook and no alert. **Open.** |
| ~~Certificate expiry~~ | **Closed — A64-028.6A.** `arena64_certificate_expiry_timestamp_seconds` is read from the certificate on disk by the worker, which mounts `/etc/letsencrypt` read-only. Three rules: expiring inside fourteen days, expired, and absent. The signal is deliberately not the renewal job's exit status — a job that reports success and writes nothing produces no failure log and an expiring certificate. |
| Email provider health | The metric exists; the rule does not. A threshold separating "the provider is down" from "several addresses bounced" needs a baseline this deployment has not produced. Guessing one would be the false-positive source §7 exists to avoid. **Open.** |
| Distributed tracing | No spans. `correlation_id` links a causal chain across services in the logs, which is the cheap 80%. **Not planned.** |
| Analytics read path | The pipeline's ingest is measured; the dashboards' query latency is not. **Open.** |

---

## Related Documents

- [`runbooks.md`](./runbooks.md) — what to do when one of these fires
- [`deployment.md`](./deployment.md) — the topology these metrics describe
- [`production-hardening.md`](./production-hardening.md) — the risk register
- [`../05-operations/performance.md`](../05-operations/performance.md) — the measurements the thresholds refer to
