import { RouterProvider } from "@tanstack/react-router";

import { createAdminRouter } from "@/app/router";
import { I18nProvider } from "@/shared/i18n";

/**
 * The admin console's root — A64-024.2.
 *
 * The authorization gate moved into the router (`ProtectedLayout`), so this
 * is composition and nothing else: translations, then routes. A64-024.1
 * had the gate here because there was one page; with seven routes the
 * check belongs at the parent route, where a section added later inherits
 * it by being a child.
 *
 * The router is injectable so a test can drive a memory history and assert
 * where the console navigated to — `window.location` cannot answer that.
 */
export function App({ router }: { router?: ReturnType<typeof createAdminRouter> }) {
  return (
    <I18nProvider>
      <RouterProvider router={router ?? createAdminRouter()} />
    </I18nProvider>
  );
}
