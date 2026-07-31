import { getTranslations } from "next-intl/server";

import { Button } from "@/components/ui/button";
import { Link } from "@/i18n/navigation";

/**
 * Fires for any unmatched path under a recognised locale segment (e.g.
 * `/en/does-not-exist`). Genuinely unmatched paths that don't even carry a
 * locale prefix fall through to the root `app/not-found.tsx` instead,
 * which cannot rely on a resolved locale — see that file's docstring.
 */
export default async function LocaleNotFound() {
  const t = await getTranslations("notFound");
  const tc = await getTranslations("common");

  return (
    <div className="mx-auto flex min-h-[60vh] max-w-6xl flex-col items-start justify-center gap-4 px-4">
      <h1 className="text-3xl font-semibold tracking-tight">{t("title")}</h1>
      <p className="text-muted-foreground max-w-prose">{t("description")}</p>
      <Button asChild>
        <Link href="/">{tc("goHome")}</Link>
      </Button>
    </div>
  );
}
