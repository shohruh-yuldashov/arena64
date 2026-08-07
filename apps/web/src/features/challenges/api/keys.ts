/**
 * Every cache key the challenge feature owns — A64-022.5 §15.
 *
 * ## Two keys, because there are two endpoints
 *
 * `incoming` and `outgoing` are separate reads with separate actions, and
 * they move independently: declining an incoming challenge changes nothing
 * about what the viewer has sent. A single `challenges` key would refetch
 * both lists on every write, which is a doubled request on the page where
 * both are already on screen.
 *
 * ## The invalidation matrix
 *
 * | Mutation | Invalidates |
 * | --- | --- |
 * | create | `outgoing` — and nothing else. The recipient's incoming list is on **their** client, and no key here can reach it |
 * | accept | `incoming`, plus `matchmaking`'s two reads — the acceptance created a match, and the lobby's cache is the authority on it |
 * | decline | `incoming` |
 * | cancel | `outgoing` |
 * | `notification.created` for either challenge type | **both**, because the frame says only that something happened and the client does not know which side it was on |
 *
 * Nothing here invalidates the friends list or a public profile. A
 * challenge does not change a relationship — and the eligibility a profile
 * reports (`relationship`) is about friendship, not about whether an
 * invitation is outstanding.
 */
export const challengeKeys = {
  all: ["challenges"] as const,

  /** `GET /challenges/incoming` — live challenges sent **to** the viewer. */
  incoming: () => [...challengeKeys.all, "incoming"] as const,

  /** `GET /challenges/outgoing` — live challenges the viewer **sent**. */
  outgoing: () => [...challengeKeys.all, "outgoing"] as const,
} as const;
