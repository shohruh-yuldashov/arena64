import { Button } from "@/shared/ui";

/**
 * What a user sees when this app has a defect.
 *
 * Rendered by the root error boundary, so it is the last thing between a
 * thrown error and a blank white page.
 *
 * ## Two audiences, two messages — CLAUDE.md §9.7
 *
 * The user gets a sentence they can act on and a way to retry. They do not
 * get the message, the stack, or an internal identifier: those say nothing
 * useful to them and something useful to whoever is probing the app. The
 * full detail went to `reportError` before this rendered.
 *
 * `reset` re-renders the subtree rather than reloading the document, so a
 * transient failure costs a click instead of the whole session.
 */
export default function UnexpectedErrorPage({ reset }: { reset: () => void }) {
  return (
    <section
      role="alert"
      className="mx-auto flex max-w-md flex-col items-center gap-4 py-24 text-center"
    >
      <h1 className="text-2xl font-semibold">Something went wrong</h1>
      <p className="text-muted-foreground text-sm">
        The problem has been recorded. Trying again often works; if it does not, reload the
        page.
      </p>
      <Button onClick={reset}>Try again</Button>
    </section>
  );
}
