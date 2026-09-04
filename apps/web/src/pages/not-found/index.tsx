import { Link } from "@tanstack/react-router";

import { useTranslation } from "@/shared/i18n";
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
 *
 * ## Translated, which it was not — A64-026.5 §44.3
 *
 * The three sentences here were English string literals in a product that
 * ships three languages, on the one page every mistyped public URL lands
 * on. The `notFound` keys already existed in all three catalogues and had
 * never been read by anything.
 *
 * The way out said "Back to the lobby", and `/` is not a lobby in either
 * of its two forms: the landing page for a visitor, the product home for a
 * signed-in player. It names the product instead, which is true of both.
 */
export default function NotFoundPage() {
  const { t } = useTranslation();

  return (
    <section className="mx-auto flex max-w-md flex-col items-center gap-4 py-24 text-center">
      <p className="text-muted-foreground text-sm font-medium tracking-widest uppercase">
        {t("notFound.code")}
      </p>
      <h1 className="text-2xl font-semibold">{t("notFound.title")}</h1>
      <p className="text-muted-foreground text-sm">{t("notFound.description")}</p>
      <Button asChild className="min-h-11">
        <Link to="/">{t("notFound.back")}</Link>
      </Button>
    </section>
  );
}
