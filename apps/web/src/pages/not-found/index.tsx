import { Link } from "@tanstack/react-router";

import { Button } from "@/shared/ui";

/**
 * Every path the router does not recognise.
 *
 * Wired as the root route's `notFoundComponent`, so it covers unknown
 * paths at any depth rather than only a literal `/404` a user would have
 * to be sent to. A redirect would have been worse: it rewrites the address
 * bar, so the user loses the URL that was wrong and cannot see the typo.
 *
 * `<h1>` and a real `<Link>` on purpose — the page is a document with a
 * heading, and the way out is a navigation, not a button that calls
 * `navigate()`. A screen reader announces the first as a landmark and the
 * second as a link; a `div` and an `onClick` announce neither.
 */
export default function NotFoundPage() {
  return (
    <section className="mx-auto flex max-w-md flex-col items-center gap-4 py-24 text-center">
      <p className="text-muted-foreground text-sm font-medium tracking-widest uppercase">404</p>
      <h1 className="text-2xl font-semibold">This page does not exist</h1>
      <p className="text-muted-foreground text-sm">
        The address may be mistyped, or whatever was here has moved.
      </p>
      <Button asChild>
        <Link to="/">Back to the lobby</Link>
      </Button>
    </section>
  );
}
