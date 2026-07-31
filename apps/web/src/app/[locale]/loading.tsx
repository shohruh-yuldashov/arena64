import { getTranslations } from "next-intl/server";

import { Skeleton } from "@/components/ui/skeleton";

/**
 * The Suspense fallback for every route under this locale segment that
 * doesn't define a more specific `loading.tsx` of its own. A Server
 * Component — no interactivity is needed for a loading state, so it ships
 * no client JavaScript.
 */
export default async function LocaleLoading() {
  const t = await getTranslations("common");

  return (
    <div
      role="status"
      aria-label={t("loading")}
      className="mx-auto flex max-w-6xl flex-col gap-4 px-4 py-24"
    >
      <Skeleton className="h-10 w-2/3" />
      <Skeleton className="h-5 w-full max-w-prose" />
      <Skeleton className="h-5 w-5/6 max-w-prose" />
    </div>
  );
}
