/**
 * Where an unexpected failure goes.
 *
 * One function, because there must be exactly one seam between "something
 * broke" and "somebody finds out". Today it writes to the console; when
 * telemetry ships it becomes the one file that changes, and every caller —
 * the error boundary, the query cache, a future mutation — is already
 * pointing at it.
 *
 * **Never throws.** A reporting failure must not fail the thing it was
 * reporting on (CLAUDE.md §8.10), so the body is guarded.
 */
export type ErrorContext = Record<string, unknown>;

export function reportError(error: unknown, context: ErrorContext = {}): void {
  try {
    console.error("[arena64]", error, context);
  } catch {
    /* A console that throws is not a reason to fail a request. */
  }
}
