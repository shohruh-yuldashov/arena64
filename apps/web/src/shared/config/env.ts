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
  VITE_API_URL: z
    .url()
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
