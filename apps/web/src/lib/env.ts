import { z } from "zod";

/**
 * Typed, validated environment configuration — the frontend counterpart to
 * `apps/api/app/config/settings.py`. A module reads `env`, never
 * `process.env` directly, so the whole configuration surface is
 * enumerable from one file (dependency-injection.md §1.6's rule, applied
 * here even though this app has no DI container of its own).
 *
 * Only `NEXT_PUBLIC_*` variables are readable in the browser bundle — this
 * file must never validate a secret, because there is no such thing as a
 * frontend secret; anything read here is public by construction.
 */
const envSchema = z.object({
  NEXT_PUBLIC_API_URL: z
    .string()
    .url()
    .transform((url) => url.replace(/\/+$/, "")), // no trailing slash — services/api-client.ts joins paths onto this directly
});

function loadEnv() {
  const parsed = envSchema.safeParse({
    NEXT_PUBLIC_API_URL: process.env.NEXT_PUBLIC_API_URL,
  });

  if (!parsed.success) {
    // Fails the build/boot loudly, the same posture as the backend's
    // DI-06: a missing or malformed value must never ship silently and
    // surface as a broken `fetch` call three layers away from its cause.
    throw new Error(
      `Invalid environment configuration:\n${parsed.error.issues
        .map((issue) => `  - ${issue.path.join(".")}: ${issue.message}`)
        .join("\n")}`,
    );
  }

  return parsed.data;
}

export const env = loadEnv();
