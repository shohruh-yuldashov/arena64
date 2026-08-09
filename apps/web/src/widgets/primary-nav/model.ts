import { type RegisteredRouter, type RouteIds, useRouterState } from "@tanstack/react-router";

import type { TranslationKey } from "@/shared/i18n";

/**
 * Every route id the tree declares, from the router the app registers.
 *
 * Typed rather than `string`, so a section claiming a route that does not
 * exist — or one renamed out from under it — is a compile error rather than
 * a section that silently never highlights.
 */
type AppRouteId = RouteIds<RegisteredRouter["routeTree"]>;

/** The same set, minus the one id that is not a destination. */
type AppRoutePath = Exclude<AppRouteId, "__root__">;

/**
 * The player's primary navigation — A64-025.3 §6.
 *
 * ## Four sections, not twenty-five routes
 *
 * `specs/product-experience.md` §3.3 recorded that navigation had ended up
 * inside `SessionMenu` — a component named after the account, holding the
 * whole product. This is the taxonomy that replaces it, and it is derived
 * from the five questions §6 asks a header to answer:
 *
 *     Where do I start a game?      Play
 *     Where are the tournaments?    Tournaments
 *     Where are my friends?         Social
 *     Where are my past games?      History
 *     Where is my account?          the account menu, not here
 *
 * Four items, so the fifth question is answered somewhere the first four do
 * not compete with. A header with every route in it answers none of them.
 *
 * ## Sections own more routes than they link to
 *
 * `Social` links to `/friends`, and `SocialNav` — which already existed and
 * is unchanged — carries requests, challenges, blocked and search inside
 * it. So `/search` is not a top-level destination, exactly as §6 allows:
 * it is where you look for a person, which is what that whole section is
 * about.
 *
 * `routeIds` is therefore a list of the routes a section owns, not a prefix.
 * A section is current when the router says one of them matched.
 */
export interface NavSection {
  /** Where the header link goes. */
  readonly to: AppRoutePath;
  readonly label: TranslationKey;
  /**
   * The **route ids** this section owns, in the router's own vocabulary.
   *
   * Ids, not paths and not prefixes. `/games/history` and
   * `/games/$matchId` are two routes that share a prefix and belong to two
   * sections, and the router already resolves which one a URL is —
   * `router.state.matches` on `/games/history` is exactly
   * `["__root__", "/games/history"]`, never the parameterised one.
   *
   * Asking `matchRoute({ to: "/games/$matchId" })` instead was tried and is
   * wrong: it answers "could this pattern match", and `$matchId` happily
   * swallows the literal `history`. The matched ids answer "what did match",
   * which is the question.
   */
  readonly routeIds: readonly AppRouteId[];
}

export const NAV_SECTIONS: readonly NavSection[] = [
  {
    to: "/play",
    label: "play.nav.play",
    // A live game is *playing*, so it lights the Play section — §7 asks for
    // this to be argued rather than assumed. The argument: a player in a
    // match who glances at the header should see where they are, and the
    // only honest answer is the section they are in the middle of using.
    //
    // `/games/$matchId` and not `/games` with `fuzzy`: `/games/history` is
    // its own section below, and the router already tells the two apart —
    // a static segment wins over a parameter. Asking the router is what
    // keeps this correct; a `startsWith("/games")` would light both.
    routeIds: ["/play", "/games/$matchId", "/games/$matchId/replay"],
  },
  {
    to: "/tournaments",
    label: "tournament.nav",
    routeIds: ["/tournaments", "/tournaments/$tournamentId"],
  },
  {
    to: "/friends",
    label: "social.nav.friends",
    routeIds: [
      "/friends",
      "/friends/requests",
      "/friends/blocked",
      "/challenges",
      "/search",
      // A player's public page is a social destination — you arrive at one
      // from a friends list or a search, never from the lobby.
      "/players/$username",
    ],
  },
  {
    to: "/games/history",
    label: "history.title",
    routeIds: ["/games/history"],
  },
];

/**
 * Which section the current route belongs to, or `null` outside all of them.
 *
 * Asks the **router which routes matched**, which is the strongest form of
 * the question §7 asks for: not "does this path look like that one" but
 * "what did this URL resolve to". The route tree has already done the
 * ranking, so a static segment beating a parameter is not something this
 * has to reimplement or get wrong.
 *
 * Returns the section's `to`, which is the identity the header renders by.
 */
export function useActiveSection(): AppRoutePath | null {
  const matchedIds = useRouterState({ select: (state) => state.matches.map((m) => m.routeId) });

  for (const section of NAV_SECTIONS) {
    if (section.routeIds.some((id) => matchedIds.includes(id))) return section.to;
  }
  return null;
}
