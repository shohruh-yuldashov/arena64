import { Link } from "@tanstack/react-router";

import { useTranslation } from "@/shared/i18n";
import { cn } from "@/shared/lib/cn";

/**
 * The Arena64 wordmark, and the way home — A64-025.3 §5.
 *
 * It was a `<span>`: the product's name sat in the header with no
 * affordance, and `/` was reachable only by editing the address bar
 * (`specs/product-experience.md` P3-1). A wordmark that does not go home is
 * the one convention on the web that every visitor already knows and this
 * product was not honouring.
 *
 * A real `Link`, so it is a link to a screen reader, opens in a new tab on
 * middle click, and shows its destination on hover — none of which a `div`
 * with an `onClick` does.
 *
 * ## A wordmark, not a logo
 *
 * There is no logo asset in the repository and A64-025.3 is not the task
 * that designs one — brand is A64-025.2's, and inventing a mark here would
 * be work the next task has to undo. Text set in the app's own type is the
 * honest placeholder.
 *
 * A64-025.9 §18.7 set that type in the brand gradient — one of the three
 * places the gradient is allowed. It degrades to the solid brand colour
 * where `background-clip: text` is unsupported, never to invisible text.
 *
 * ## One definition, three sizes — A64-026.2 §41.1
 *
 * The treatment was written out three times: here, in `auth-shell`, and in
 * the landing page's header, differing only in whether the type was `sm` or
 * `base`. Three copies of a brand is three places to find when the brand
 * changes, and the drift had already started — the landing header was a
 * step larger than the other two with nothing recording why.
 *
 * So the size is a prop with three values and the treatment is stated once.
 * `wordmarkClass` exists for the one caller that needs the *mark* without
 * this component's link, which is the auth front door: it is already inside
 * a card that is itself the way home, and a second link to `/` there would
 * be two controls doing one thing.
 */

/** The wordmark's type, at the three sizes this product uses it. */
const SIZE_CLASS = {
  sm: "text-sm",
  base: "text-base",
  lg: "text-lg",
} as const;

export type BrandSize = keyof typeof SIZE_CLASS;

/**
 * The wordmark's own classes, without a link around it.
 *
 * Exported so a caller that cannot use `Brand` still cannot invent a fourth
 * treatment — the thing this task exists to stop.
 */
export function wordmarkClass(size: BrandSize = "sm"): string {
  return cn("brand-gradient-text font-semibold tracking-tight", SIZE_CLASS[size]);
}

export function Brand({ size = "sm" }: { size?: BrandSize }) {
  const { t } = useTranslation();

  return (
    <Link
      to="/"
      className="focus-visible:ring-ring flex min-h-11 items-center rounded-md pr-2 focus-visible:ring-2 focus-visible:outline-none"
      aria-label={t("layout.home")}
    >
      {/* The gradient is on the text, not the link: the link keeps its own
          focus ring, which a clipped background would otherwise swallow. */}
      <span className={wordmarkClass(size)}>{t("layout.title")}</span>
    </Link>
  );
}
