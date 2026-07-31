import { notFound } from "next/navigation";

/**
 * A catch-all for every path under a valid locale that no other route
 * file matches. Without this, Next.js has no route to render for e.g.
 * `/ru/does-not-exist` at all, and falls all the way through to the root
 * `app/not-found.tsx` — English-only by necessity (see its docstring),
 * which would make a Russian-speaking player land on an English 404.
 *
 * This route exists purely to be matched and immediately call
 * `notFound()`, which — because it runs *inside* the `[locale]` segment's
 * tree — correctly triggers `[locale]/not-found.tsx` instead.
 */
export default function CatchAll(): never {
  notFound();
}
