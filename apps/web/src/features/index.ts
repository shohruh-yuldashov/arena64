/**
 * Domain-specific feature slices — empty by design.
 *
 * This task (A64-007) builds foundation only; no feature (`auth`, `game`,
 * `friends`, and so on — see the task's "Do NOT implement" list) is
 * implemented here.
 *
 * When the first feature is added, it is a directory under this one,
 * mirroring the backend bounded context it talks to (architecture.md §15):
 *
 *     features/<name>/
 *         components/    feature-specific UI, not shared elsewhere
 *         hooks/          feature-specific data hooks (wrapping TanStack Query)
 *         api.ts          calls into services/api-client.ts for this feature
 *         types.ts        the feature's own domain types
 *
 * `components/` (this app's top level) stays reserved for presentational,
 * domain-ignorant UI — buttons, layout chrome — the same rule
 * `packages/ui` follows for components shared across `apps/web` and
 * `apps/admin` (architecture.md §15's `packages/ui` row).
 */
export {};
