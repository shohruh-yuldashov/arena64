import Link from "next/link";

/**
 * The true root's fallback — reached only when a request matches no route
 * at all, not even the `[locale]` segment (middleware.ts's matcher
 * rewrites almost everything into a locale-prefixed path, so this is a
 * rare safety net, not the common 404). There is no `app/layout.tsx`
 * above this file — the root layout lives at `app/[locale]/layout.tsx`
 * and only wraps routes that resolved a locale — so this file supplies
 * its own `<html>`/`<body>`, the same requirement and the same reasoning
 * as `global-error.tsx`. Plain, neutral, English-only for the same
 * reason: no locale has been resolved for this request.
 *
 * Uses plain `next/link`, not `@/i18n/navigation`'s locale-aware `Link` —
 * this file has no locale to be aware of. Middleware still resolves the
 * unprefixed `/` it points at to the right locale on arrival.
 */
export default function RootNotFound() {
  return (
    <html lang="en">
      <body
        style={{
          display: "flex",
          minHeight: "100vh",
          flexDirection: "column",
          alignItems: "center",
          justifyContent: "center",
          gap: "1rem",
          fontFamily: "system-ui, sans-serif",
          padding: "1.5rem",
          textAlign: "center",
        }}
      >
        <h1 style={{ fontSize: "1.5rem", fontWeight: 600 }}>Page not found</h1>
        <p style={{ color: "#666", maxWidth: "32rem" }}>
          The page you&apos;re looking for doesn&apos;t exist or has moved.
        </p>
        <Link
          href="/"
          style={{
            borderRadius: "0.375rem",
            border: "1px solid #ccc",
            padding: "0.5rem 1rem",
            textDecoration: "none",
            color: "inherit",
          }}
        >
          Go home
        </Link>
      </body>
    </html>
  );
}
