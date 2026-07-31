# CLAUDE.md — Engineering Instruction Manual

> **Audience:** every Claude Code session and every human contributor working on Arena64.
> **Authority:** this document defines *how* work is done. It outranks habit, convenience,
> and any prompt in `prompts/`. Where it conflicts with an explicit user instruction, the
> user wins — but say which rule is being set aside and why.
> **Scope:** engineering practice only. No project-specific implementation detail belongs
> here; that lives in `docs/01-architecture/`, `docs/03-backend/`, `docs/04-frontend/`,
> and `specs/`.

---

## 1. Development Philosophy

1. **Correctness first, then clarity, then performance.** Optimise in that order. A fast
   wrong answer has negative value.
2. **Understand before changing.** Read the surrounding code, the relevant spec, and the
   relevant decision record before writing a line. Changes made without context are guesses.
3. **The requested scope is the deliverable.** Do not silently narrow it, widen it, or
   substitute a different problem. If the request seems wrong, say so in a sentence and
   then deliver what was asked under stated assumptions.
4. **Boring solutions win.** Prefer the obvious approach a competent engineer would reach
   for. Novelty must be justified by a measured need, recorded in an ADR.
5. **Write for the next reader.** Code is read far more often than it is written. Optimise
   the reading experience of someone unfamiliar with the change.
6. **Leave the code better than you found it** — but confine cleanup to what the task
   touches. Unrelated refactors belong in their own change.
7. **No speculative generality.** Build for the requirement in front of you. Abstractions
   are earned by a third concrete use case, not predicted by the first.
8. **Finish the work.** Partial implementations, stubbed branches, and TODOs left in place
   of logic are not deliverables. If part of the scope is blocked, complete everything else
   and state plainly what was left out and why.
9. **Report honestly.** If tests fail, show the output. If a step was skipped, say so.
   Never describe unverified work as verified.

---

## 2. Coding Principles

### 2.1 Structure

- **Single responsibility.** A function, class, or module has one reason to change. If its
  description needs "and", split it.
- **Small units.** Prefer functions that fit on one screen. Length is a symptom, not the
  disease — the real target is one level of abstraction per function.
- **Depth over breadth.** Prefer a few well-named modules with meaningful interfaces over
  many thin pass-through layers.
- **Explicit over implicit.** No hidden global state, no action at a distance, no magic
  that a reader cannot trace from the call site.
- **Composition over inheritance.** Use inheritance only for genuine substitutable
  is-a relationships.
- **Immutability by default.** Mutate only where it is the clearer or measurably faster
  option, and confine mutation to the narrowest possible scope.
- **Pure core, effectful edges.** Keep business rules free of I/O so they are trivially
  testable. Push network, disk, clock, and randomness to the boundaries and inject them.

### 2.2 Naming

- Names state intent, not mechanism: `expiresAt` over `timestamp2`.
- No abbreviations beyond established domain vocabulary; no single-letter names outside
  tight numeric loops.
- Booleans read as assertions: `isActive`, `hasExpired`, `canJoin`.
- Functions are verb phrases; variables, classes, and types are noun phrases.
- Consistent vocabulary across the codebase — one concept, one word, everywhere.

### 2.3 Control Flow

- Guard clauses over nested conditionals; return early.
- Keep nesting shallow — three levels is a warning sign.
- Handle the exceptional case first and leave the happy path unindented at the bottom.
- No boolean parameters that select behaviour; use two functions or a named enum.

### 2.4 Types and Contracts

- Type every public boundary: function signatures, module exports, API payloads, and
  persisted shapes.
- Make illegal states unrepresentable. Prefer a precise union or enum over a loose string.
- Never suppress a type error to move on. Fix the model, or record the exception with a
  comment stating why it is safe.
- Validate all external input at the boundary — user input, network responses, environment
  configuration, and file contents. Inside the boundary, trust the types.

### 2.5 Comments

- Comment **why**, never **what**. The code already states what it does.
- Document non-obvious constraints, chosen trade-offs, and links to the spec or ADR that
  justifies an unusual approach.
- Delete commented-out code. Version control is the archive.
- Keep comments truthful — a stale comment is worse than none. Update comments in the same
  change as the code they describe.
- Match the comment density of the surrounding file.

### 2.6 Dependencies

- Every new dependency is a long-term liability: audit maintenance status, licence,
  transitive weight, and security history before adding one.
- Do not add a dependency for what the standard library or an existing dependency does.
- Pin versions and update deliberately, never incidentally.
- Wrap third-party libraries at the boundary when they would otherwise leak their types
  through the domain layer.

### 2.7 Prohibited

- Copy-pasted logic that should be shared, and shared abstractions that only accidentally
  coincide. Duplication is cheaper than the wrong abstraction.
- Dead code, unreachable branches, and unused exports.
- Debug output, scratch files, or commented experiments left in a change.
- Hardcoded credentials, tokens, endpoints, or environment-specific values.
- Silent failures: an empty `catch`, a swallowed rejection, a default that masks an error.

---

## 3. Architecture Rules

1. **Respect layer boundaries.** Dependencies point inward: transport → service → domain,
   and data access is reached through its declared abstraction. A layer never imports from
   the layer above it.
2. **Domain logic stays framework-free.** Business rules must not import HTTP, ORM, or UI
   framework types. If the framework changed tomorrow, the domain should not.
3. **Depend on abstractions at boundaries.** Inject collaborators; do not construct them
   inside the consumer. See `docs/03-backend/dependency-injection.md`.
4. **One source of truth per concept.** A rule, constant, or shape is defined once and
   imported. Divergent copies are defects waiting to happen.
5. **Shared code earns its place.** Code moves into a shared package when two or more
   consumers genuinely need the same behaviour — not in anticipation.
6. **Contracts are explicit and versioned.** API and event payloads are stated in a spec
   before implementation, and changed additively wherever possible.
7. **Design for failure.** Every remote call has a timeout, a defined failure behaviour,
   and a bounded retry policy where retrying is safe. Assume dependencies will be slow or
   unavailable, not merely up or down.
8. **Idempotency where operations can repeat.** Any handler that may be retried or
   redelivered must tolerate duplicate execution.
9. **Stateless services.** Keep request-scoped state out of process memory so instances
   scale horizontally and restart freely.
10. **Significant decisions become ADRs.** Any choice that constrains future work is
    recorded in `docs/07-decisions/` using `templates/architecture-decision.md`.
11. **No architectural drift.** If the implementation must diverge from the documented
    architecture, change the document in the same pull request, or do not diverge.

---

## 4. Documentation Rules

1. **Documentation is part of the change, not a follow-up.** A pull request that alters
   behaviour and leaves the documentation stale is incomplete.
2. **Every document declares its status and owner.** Unowned documents rot.
3. **Write once, link everywhere.** Never restate content that exists elsewhere; reference
   it by path. Duplicated documentation diverges.
4. **Documents state intent and contracts, not code.** If a snippet is needed to be
   understood, keep it minimal and illustrative.
5. **Placement is deliberate:**

   | Content | Location |
   | --- | --- |
   | Product direction and milestones | `docs/00-overview/` |
   | System-wide and cross-cutting design | `docs/01-architecture/` |
   | Engineering process and conventions | `docs/02-development/` |
   | Backend layer guidance | `docs/03-backend/` |
   | Frontend layer guidance | `docs/04-frontend/` |
   | Decision records | `docs/07-decisions/` |
   | Per-feature behaviour and contracts | `specs/` |
   | Reusable document skeletons | `templates/` |
   | Reusable agent prompts | `prompts/` |
   | Module-local orientation | that module's `README.md` |

6. **Start from the template.** New specs, ADRs, and module READMEs begin from
   `templates/`, so structure is consistent and sections are not forgotten.
7. **Use absolute repository paths** when referring to files, so references survive moves
   between documents.
8. **Prefer tables and lists to prose** for anything enumerable — reviewers scan.
9. **Record dates absolutely** (`2026-07-31`), never relatively ("last week").
10. **Never document a secret value.** Document the name and purpose of a configuration
    variable, never its contents.

---

## 5. Git Rules

Detailed process lives in `docs/02-development/git-workflow.md`. These rules are binding
regardless of the workflow chosen.

1. **Never commit directly to the default branch.** Branch, then open a pull request.
2. **One logical change per commit.** A commit compiles, passes tests, and can be reverted
   on its own.
3. **One concern per pull request.** Do not mix refactoring with behaviour change — the
   reviewer cannot separate them, so neither gets reviewed properly.
4. **Commit messages explain why.** Imperative subject under ~72 characters, blank line,
   then a body covering motivation and consequences. The diff already shows what changed.
5. **Never rewrite published history.** Rebase your own unpushed work freely; never force
   over a branch others may have pulled.
6. **Never commit** secrets, credentials, `.env` files, build output, dependency
   directories, editor state, or large binaries. If a secret is committed, treat it as
   compromised: rotate it, then purge it.
7. **Keep the working tree clean.** Unrelated formatting churn obscures the real change.
8. **Commit or push only when asked.** Never assume that finishing work implies publishing it.
9. **Pull requests use `templates/pull-request.md`** and link the spec, issue, or ADR they
   implement.
10. **Green before merge.** Lint, type checks, and the full test suite pass; review approval
    is required.

---

## 6. Testing Rules

Strategy and tooling live in `docs/02-development/testing.md`. These rules are binding.

1. **Behaviour change requires a test.** New behaviour gets a test that would fail without
   it; a bug fix gets a regression test that fails before the fix and passes after.
2. **Test behaviour, not implementation.** Assert on observable outcomes through public
   interfaces. Tests coupled to internals block refactoring, which is exactly backwards.
3. **Follow the pyramid.** Many fast unit tests, fewer integration tests, a thin layer of
   end-to-end tests over critical journeys.
4. **Tests are deterministic.** No dependence on wall-clock time, timezone, locale,
   network, random seeds, or execution order. Inject clocks and randomness.
5. **Zero tolerance for flaky tests.** A test that fails intermittently is a broken test.
   Fix it or delete it — never re-run until green, and never disable it silently.
6. **Arrange–Act–Assert,** with one logical assertion per test and a name that states the
   scenario and the expectation.
7. **Isolation.** Each test creates its own state and cleans up. Tests must pass in any
   order and in parallel.
8. **Mock only what you own or what is genuinely external** — the network, the clock, third
   party services. Mocking internal collaborators everywhere produces tests that pass while
   the system is broken.
9. **Cover the edges:** empty, boundary, maximum, malformed, concurrent, and failure inputs.
   The happy path is the least interesting test.
10. **Coverage is a diagnostic, not a target.** High coverage with weak assertions is
    self-deception.
11. **Never weaken a test to make it pass.** If a test fails, either the code is wrong or
    the test encodes an outdated requirement — decide which, and say which.
12. **Run the suite before declaring completion,** and report the actual result.

---

## 7. Refactoring Rules

1. **Refactoring never changes behaviour.** If behaviour changes, it is not a refactor —
   label it accurately.
2. **Tests first.** Establish passing coverage over the affected behaviour *before*
   restructuring. Without it you are rewriting, not refactoring.
3. **Separate commits, separate pull requests.** Never bundle a refactor with a feature or
   a fix.
4. **Small, reversible steps.** Each step keeps the suite green. Large rewrites that are
   green only at the end are unreviewable and unrevertable.
5. **Refactor with a reason.** Valid reasons: the change you need is hard to make safely,
   duplication has been demonstrated three times, a measurement identified a hotspot, or
   the structure contradicts a documented rule. "It looks nicer" is not a reason.
6. **Do not refactor code you are not asked to touch,** and never opportunistically
   restyle files you happen to open.
7. **Preserve public interfaces** unless the change is the point. When a break is
   necessary, deprecate, provide a migration path, then remove.
8. **Delete rather than deprecate** when nothing depends on it. Dead code carries
   maintenance cost forever.
9. **Match the surrounding code.** New code adopts the conventions, naming, and idioms of
   the file it lives in, even where personal preference differs.

---

## 8. Logging Rules

1. **Structured, machine-parseable logs.** Key–value or JSON fields, never interpolated
   prose that must be regex-parsed later.
2. **Use levels with discipline:**

   | Level | Use for |
   | --- | --- |
   | `ERROR` | A failure that requires human attention; something is broken |
   | `WARN` | Unexpected but handled; degraded behaviour worth noticing |
   | `INFO` | Significant business events and lifecycle transitions |
   | `DEBUG` | Diagnostic detail for development; off in production |
   | `TRACE` | Fine-grained flow; enabled deliberately and temporarily |

3. **Never log secrets or personal data:** passwords, tokens, session identifiers, keys,
   full payment or contact details. Redact at the logging boundary, not at each call site,
   so redaction cannot be forgotten.
4. **Every log carries correlation context** — request or trace identifier, and the actor
   where applicable — so a single interaction can be reconstructed across services.
5. **Log at the boundary, not at every step.** Logs on every line are noise; noise hides
   the incident.
6. **Log the exception, not just the message.** Include the type, message, and stack, plus
   the identifiers needed to reproduce.
7. **Logs are for operators.** Write what someone diagnosing an incident at 3am needs:
   what failed, for whom, with what input, and what happened next.
8. **Never log inside tight loops or hot paths** without sampling or rate limiting.
9. **No `print`-style debugging in committed code.** Use the logger, at the right level.
10. **Logging never changes behaviour** and never throws. A logging failure must not fail
    the request.

---

## 9. Error Handling Rules

1. **Fail fast and loudly at boundaries.** Validate input at the edge and reject invalid
   requests immediately with a precise, actionable message.
2. **Never swallow errors.** No empty catch blocks, no ignored rejections, no `except:
   pass`. Handle, translate, or propagate — and choose deliberately.
3. **Catch only what you can handle.** If the current layer cannot make a decision about
   an error, let it propagate to one that can.
4. **Preserve the cause.** When wrapping an error, chain the original so the root cause and
   stack survive. Never discard context to produce a tidier message.
5. **Use a typed error taxonomy.** Distinguish validation errors, not-found, conflict,
   permission, and unexpected internal failures so callers branch on type, not string
   matching.
6. **Errors are part of the contract.** Document the failure modes of any public interface
   alongside its success behaviour.
7. **Separate the two audiences.** Users get a clear, safe message describing what happened
   and what to do; operators get the full diagnostic detail in logs. Never leak stack
   traces, internal identifiers, or query text to a client.
8. **Distinguish expected from exceptional.** A resource that legitimately may not exist is
   a normal outcome to model in the return type, not an exception to throw.
9. **Clean up deterministically.** Release connections, locks, files, and transactions on
   every path, including the failure path.
10. **Retry only what is safe.** Retry idempotent operations on transient failures with
    bounded attempts and exponential backoff with jitter. Never retry a validation error,
    an authorization failure, or a non-idempotent write.
11. **Degrade gracefully.** When a non-critical dependency fails, prefer reduced
    functionality over total failure — and make the degradation observable.
12. **Never leave the system in a partial state.** Use transactions or compensating actions
    so a failed multi-step operation does not persist half its effects.

---

## 10. Performance Rules

1. **Measure before optimising.** Profile against a realistic workload and identify the
   actual bottleneck. Optimisation without measurement is superstition — and usually
   targets the wrong code.
2. **Correctness is never traded for speed.** A faster wrong result is a defect.
3. **Algorithmic complexity first.** A better data structure or access pattern beats
   micro-optimisation by orders of magnitude. Know the complexity of hot paths.
4. **Eliminate N+1 access patterns.** Batch, join, or pre-load. Repeated per-item queries
   or requests inside a loop are the single most common cause of slow endpoints.
5. **Bound everything unbounded.** Every list endpoint paginates; every query has a limit;
   every queue, cache, and connection pool has a maximum. Unbounded growth is an outage
   waiting for enough traffic.
6. **Set explicit timeouts** on every I/O operation. No call waits forever.
7. **Cache deliberately.** A cache without a documented invalidation rule is a source of
   stale data. Record layer, key, TTL, and invalidation trigger before adding one; see
   `docs/01-architecture/caching.md`.
8. **Do expensive work off the request path.** Long-running or non-essential work belongs
   in a background job, not in the user's latency budget.
9. **Optimise the critical path.** Interactive and realtime paths deserve scrutiny; a
   nightly job that takes an extra second does not.
10. **Define budgets and enforce them.** State the latency, payload, query-count, and
    bundle-size targets that matter, and assert them in CI where practical.
11. **Every optimisation carries evidence.** Include the before and after measurement in
    the pull request. If it cannot be measured, it is not an optimisation.
12. **Prefer clear code until proven costly.** Readability is the default; complexity is
    purchased with data.

---

## 11. Working Agreement for AI Sessions

- Read this document, `docs/02-development/coding-standards.md`, and the relevant spec
  before making changes.
- Follow the repository's existing conventions over general preference.
- Do not create files that were not asked for — no unsolicited documentation, no example
  files, no scratch scripts left in the tree.
- Do not install dependencies, run migrations, push commits, or take other outward-facing
  or hard-to-reverse actions without explicit instruction.
- State assumptions explicitly when a request is ambiguous, and continue rather than
  stalling — unless proceeding wrongly would be unsafe or waste the work.
- Report what was actually done, including what failed and what was skipped.

---

## Related Documents

- `docs/02-development/coding-standards.md` — language-level conventions
- `docs/02-development/git-workflow.md` — branching, commits, and releases
- `docs/02-development/testing.md` — test strategy and quality gates
- `docs/02-development/folder-structure.md` — repository layout and placement rules
- `docs/01-architecture/architecture.md` — system composition and boundaries
- `docs/07-decisions/README.md` — decision record process
- `templates/` — reusable document skeletons
