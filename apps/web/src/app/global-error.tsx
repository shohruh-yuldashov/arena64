"use client";

/**
 * Catches an error thrown by the root layout itself
 * (`app/[locale]/layout.tsx`) — the one place `error.tsx` cannot help,
 * because a layout's boundary is its *parent*, and this layout has none
 * below the true root. Next.js requires this exact file, at this exact
 * path, and requires it to render its own `<html>`/`<body>`.
 *
 * Deliberately hardcoded, plain, English-only: if the root layout threw,
 * nothing below it can be trusted — including the locale that layout was
 * responsible for resolving and the providers it was responsible for
 * mounting. This is the one screen in the app that must survive without
 * either.
 */
export default function GlobalError({ reset }: { reset: () => void }) {
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
        <h1 style={{ fontSize: "1.5rem", fontWeight: 600 }}>Something went wrong</h1>
        <p style={{ color: "#666", maxWidth: "32rem" }}>
          The application failed to load. Please try again.
        </p>
        <button
          onClick={reset}
          style={{
            borderRadius: "0.375rem",
            border: "1px solid #ccc",
            padding: "0.5rem 1rem",
            cursor: "pointer",
          }}
        >
          Try again
        </button>
      </body>
    </html>
  );
}
