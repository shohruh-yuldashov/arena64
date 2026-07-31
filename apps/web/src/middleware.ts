import createMiddleware from "next-intl/middleware";

import { routing } from "@/i18n/routing";

export default createMiddleware(routing);

export const config = {
  // Runs on every path except static assets, Next internals, and files
  // with an extension (images, favicons, etc.) — those are never
  // localized and must not incur the locale-resolution cost.
  matcher: ["/((?!api|_next|_vercel|.*\\..*).*)"],
};
