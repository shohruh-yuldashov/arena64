import { z } from "zod";

/**
 * Typed, validated environment configuration — the frontend counterpart to
 * `apps/api/app/config/settings.py`. A module reads `env`, never
 * `import.meta.env` directly, so the whole configuration surface is
 * enumerable from one file (dependency-injection.md §1.6's rule, applied
 * here even though this app has no DI container of its own).
 *
 * Only `VITE_*` variables are readable in the browser bundle — this file
 * must never validate a secret, because there is no such thing as a
 * frontend secret; anything read here is public by construction.
 */
const envSchema = z.object({
  /**
   * Where the API is, from the browser's point of view.
   *
   * **A relative path by default, and that is the deployment contract.**
   * The refresh cookie is `HttpOnly` and same-site, so the page and the
   * API must share an origin: in development the Vite proxy provides that
   * (`vite.config.ts`), and in production a reverse proxy must route
   * `/api` to FastAPI. Nothing here names a host.
   *
   * An absolute URL is accepted for the one case that needs it — pointing
   * a build at a separate API host — but then the browser session will not
   * work, because the cookie will not cross origins. See
   * `specs/frontend.md` §12.
   */
  VITE_API_URL: z
    .string()
    .default("/api/v1")
    .refine(
      (value) => value.startsWith("/") || /^https?:\/\//.test(value),
      "must be a relative path such as /api/v1, or an absolute http(s) URL",
    )
    // No trailing slash — `shared/api/client.ts` joins paths onto this
    // directly, and `//matches` is a different URL from `/matches` to
    // every router that has ever existed.
    .transform((url) => url.replace(/\/+$/, "")),
});

export type Env = z.infer<typeof envSchema>;

/**
 * Validates a raw record and returns it, or throws listing every problem.
 *
 * Exported apart from `env` so the failure path is testable: Vite inlines
 * `import.meta.env` at build time, so it is not writable the way
 * `process.env` is and a test cannot provoke this by mutating it.
 */
export function parseEnv(raw: Record<string, unknown>): Env {
  const parsed = envSchema.safeParse(raw);

  if (!parsed.success) {
    // Fails the boot loudly, the same posture as the backend's DI-06: a
    // missing or malformed value must never ship silently and surface as
    // a broken request three layers away from its cause.
    throw new Error(
      `Invalid environment configuration:\n${parsed.error.issues
        .map((issue) => `  - ${issue.path.join(".")}: ${issue.message}`)
        .join("\n")}`,
    );
  }

  return parsed.data;
}

export const env: Env = parseEnv({
  VITE_API_URL: import.meta.env.VITE_API_URL,
});
