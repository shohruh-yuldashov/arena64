import type { ReactNode } from "react";

import { ThemeToggle } from "@/widgets/theme-toggle";

/**
 * The frame every page renders inside — header, main region, footer.
 *
 * ## Landmarks, and why they are not `div`s
 *
 * `<header>`, `<main>` and `<footer>` are navigation landmarks. A screen
 * reader user jumps between them directly; without them the whole page is
 * one undifferentiated region and the only way through it is linear. This
 * costs nothing and is the single highest-value accessibility decision in
 * a layout.
 *
 * ## The skip link
 *
 * First focusable element on the page, visually hidden until focused. A
 * keyboard user landing on any page can reach the content in one `Tab`
 * instead of traversing the whole header — WCAG 2.1 §2.4.1, and the one
 * thing a layout must provide because no component below it can.
 *
 * ## No business content
 *
 * No navigation to features that do not exist, no user menu, no locale
 * switcher. Each of those belongs to the phase that ships what it links
 * to; a header full of dead links is worse than a bare one.
 */
export function AppShell({ children }: { children: ReactNode }) {
  return (
    <div className="bg-background text-foreground flex min-h-full flex-col">
      <a
        href="#main"
        className="bg-background focus-visible:ring-ring sr-only rounded-md px-4 py-2 text-sm font-medium focus-visible:not-sr-only focus-visible:absolute focus-visible:top-2 focus-visible:left-2 focus-visible:z-50 focus-visible:ring-2"
      >
        Skip to content
      </a>

      <header className="border-b">
        <div className="mx-auto flex h-14 max-w-5xl items-center justify-between px-4">
          <span className="text-sm font-semibold tracking-tight">Arena64</span>
          <ThemeToggle />
        </div>
      </header>

      <main id="main" tabIndex={-1} className="mx-auto w-full max-w-5xl flex-1 px-4 py-8">
        {children}
      </main>

      <footer className="border-t">
        <div className="text-muted-foreground mx-auto flex h-12 max-w-5xl items-center px-4 text-xs">
          Arena64
        </div>
      </footer>
    </div>
  );
}
