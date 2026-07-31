import type { ReactNode } from "react";

import { SiteFooter } from "@/components/layout/site-footer";
import { SiteHeader } from "@/components/layout/site-header";

/**
 * Chrome for public, server-rendered pages — architecture.md AD-24: these
 * are the platform's SEO surface, read far more often than they change.
 * The future `(app)` route group (see its own layout) intentionally does
 * not share this chrome; an authenticated shell earns its own once it
 * exists, rather than inheriting a public header that would need
 * conditional logic bolted onto it.
 */
export default function MarketingLayout({ children }: { children: ReactNode }) {
  return (
    <div className="flex min-h-screen flex-col">
      <SiteHeader />
      <main className="flex-1">{children}</main>
      <SiteFooter />
    </div>
  );
}
