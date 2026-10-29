/**
 * Query keys for the leaderboard — A64-033.
 *
 * Per speed class *and* country: two filters are two ladders, and sharing a
 * key would show one ladder's rows under the other's heading while it
 * refetched. Not viewer-independent — the standing is the caller's own — so
 * the session provider clears all of it when the account changes.
 */
export const leaderboardKeys = {
  all: ["leaderboard"] as const,
  page: (speedClass: string, country: string | null) =>
    ["leaderboard", "page", speedClass, country ?? "all"] as const,
  standing: (speedClass: string, country: string | null) =>
    ["leaderboard", "standing", speedClass, country ?? "all"] as const,
};
