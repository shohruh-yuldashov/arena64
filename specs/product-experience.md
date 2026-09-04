# Feature Specification — Product Experience

| Field | Value |
| --- | --- |
| **Spec ID** | `SPEC-PRODUCT-EXPERIENCE` |
| **Status** | Draft — .1 audit; .2 foundation; .3 shell; .4 auth; .5 lobby; .6…​.6C game room; .7 tournament; .8 social; .9 profile |
| **Owner** | _Unassigned_ |
| **Created** | 2026-08-10 |
| **Last updated** | 2026-09-04 — A64-025.9B, home and the account menu |
| **Related specs** | [`frontend.md`](./frontend.md) — the technical frontend spec |
| **Related** | `docs/04-frontend/`, `docs/02-development/CLAUDE.md` |

---

## 1. Summary

This document holds the **player-facing experience** of Arena64: what the product
currently looks and feels like, the principles the redesign is held to, and the plan
that gets it there.

It is deliberately separate from [`frontend.md`](./frontend.md), which specifies the
*technical* frontend — stack, layers, routing, providers, per-phase implementation.
That document answers "how is it built"; this one answers "what is the experience, and
is it good enough". Neither restates the other.

**A64-025.1 was an audit.** Nothing was redesigned. The findings below are evidence
from the repository at commit `235bf28`, not impressions.

## 2. Non-goals

Stated first, because the temptation in a redesign epic is to widen.

- **No new product features.** The redesign changes how the built product is presented,
  not what it does.
- **No domain or business-logic change.** The state machines, the rules, the rating
  arithmetic and the tournament model stay exactly as they are.
- **No API expansion for decoration.** Where the UI wants a field the API does not
  offer, the answer is to redesign around what exists or to raise it as a separate,
  justified contract change — never to widen a contract because a card looked empty.
- **No new dependency without a demonstrated gap** in the current stack (§3.11).
- **`apps/admin` is out of scope.** It is a separate application with a separate
  audience and its own plain-CSS design language; unifying the two is not a goal of
  this epic.
- **No weakening of security or privacy semantics** for visual convenience. The
  HttpOnly refresh-cookie model, the verified-email gate and the privacy field rules
  are inputs to the design, not obstacles to it.

---

## 3. Current-state audit — A64-025.1

### 3.1 Surface inventory

Twenty-five routes, from `apps/web/src/app/router/routes.tsx`. Nineteen are behind
`protectedPage` (authenticated **and** verified); the rest are deliberately open.

| Surface | Route | Guard | State |
| --- | --- | --- | --- |
| Landing | `/` | none | **Developer exhibit — see P0-1** |
| Login | `/login` | anonymous-only | Built |
| Register | `/register` | anonymous-only | Built |
| Email verification | `/verify-email` | none (two modes) | Built |
| Forgot password | `/forgot-password` | none | Built |
| Reset password | `/reset-password` | none | Built |
| Own profile | `/profile` | protected | Built |
| Public profile | `/players/$username` | none | Built |
| Settings — profile | `/settings/profile` | protected | Built |
| Settings — preferences | `/settings/preferences` | protected | Built |
| Settings — privacy | `/settings/privacy` | protected | Built |
| Settings — notifications | `/settings/notifications` | protected | Built |
| Settings — sessions | `/settings/sessions` | protected | Built |
| Friends | `/friends` | protected | Built |
| Friend requests | `/friends/requests` | protected | Built |
| Blocked | `/friends/blocked` | protected | Built |
| Challenges | `/challenges` | protected | Built |
| Player search | `/search` | protected | Built |
| Lobby | `/play` | protected | Built |
| Live game | `/games/$matchId` | protected | Built |
| Replay | `/games/$matchId/replay` | protected | Built |
| Match history | `/games/history` | protected | Built |
| Notifications | `/notifications` | protected | Built |
| Tournament list | `/tournaments` | protected | Built |
| Tournament detail | `/tournaments/$tournamentId` | protected | Built |
| Not found / error | fallback | none | Built |

PWA install, update and offline notices are not routes: they are mounted globally by
`AppShell` via `widgets/pwa`.

### 3.2 Design language — better than expected, and incomplete

The questions A64-025.1 was asked, answered with evidence.

| Question | Answer | Evidence |
| --- | --- | --- |
| Design token system? | **Yes** | `apps/web/src/app/styles/globals.css` — a complete shadcn/ui "New York" neutral theme in OKLCH, light and dark, mapped to Tailwind v4 utilities via `@theme inline` |
| Primitive component layer? | **Yes, partial** | `apps/web/src/shared/ui/` — Button, Input, Card, Dialog, Avatar, Skeleton, Spinner, ErrorBoundary. Eight primitives |
| Components duplicated? | **Yes, for states** | No shared empty/error/loading component: `role="alert"` is written out 34 times across 24 files, `role="status"` 40 times across 23 |
| Arbitrary Tailwind values? | **No** | Whole-tree scan finds only justified ones: `focus-visible:ring-[3px]`, `max-h-[90dvh]`, `lg:max-w-[min(70vh,42rem)]`, `pb-[max(1.5rem,env(safe-area-inset-bottom))]` |
| Visual hierarchy consistent? | **Mostly** | Only three colour utilities appear across the tree — `text-muted-foreground` (132), `text-primary` (14), `text-destructive` (14). No raw palette colours anywhere |
| Dark/light theme? | **Yes** | `.dark` class on `<html>`, written before first paint by an inline script in `index.html`, kept in step by `apps/web/src/shared/theme/theme-context.tsx` |
| Responsive strategy consistent? | **Yes, and narrow** | One breakpoint does nearly all the work — `sm:` and `lg:`. Simple, but see §3.7 |

The discipline here is real and should be preserved. What is missing is not rigour but
**range**:

- **No brand.** Every colour in the palette has chroma `0` except `--destructive` and
  the five unused chart colours. `--primary` is near-black in light and near-white in
  dark. Arena64 currently has no colour of its own.
- **No game-semantic colour.** There is no token for win, loss, draw, your-turn,
  low-time or live. `text-primary` is doing all of that work, at whatever contrast a
  neutral primary happens to give.
- **Eight primitives is not a system.** No Badge, Select, Tabs, Tooltip, Dropdown,
  Switch, Notice or EmptyState — each of which is currently re-authored per feature.

### 3.3 Navigation and information architecture

Primary navigation lives in `widgets/session-menu`, rendered in the header by
`widgets/app-shell`. Despite the name it is not a menu: it is a flat row of buttons —
Play, Tournaments, Friends, Profile, Sign out — beside `NotificationBell` and
`ThemeToggle`.

Findings:

- **The name is wrong and the structure follows it.** A "session menu" that is
  actually the product's main navigation is a component nobody will look in when they
  need to add a nav item — which is likely why `/games/history`, `/challenges` and
  `/search` never reached it.
- **No `aria-current`.** Nothing in the header says which section is open.
- **No mobile treatment.** Only the user's *name* is hidden below `sm:`
  (`hidden … sm:inline`). All three nav buttons, the avatar, sign-out, the bell and the
  theme toggle stay in one 56px row at 360px, beside the brand.
- **The brand is not a link.** `<span>Arena64</span>` — there is no home affordance.
- **Anonymous visitors get no navigation at all** — only a Sign in button.

**Does the UI answer "where do I start a game?"** For a signed-in player, yes: Play is
the first button and the only `default`-variant one. For a first-time visitor landing
on `/`, **no** — see P0-1.

### 3.4 Authentication

Solid and not the redesign's problem. `RequireAnonymous`, `RequireAuth` and
`RequireVerifiedEmail` are composed in `apps/web/src/app/router/guards.tsx`; `/verify-email` is deliberately
unguarded because it serves both a mailed link and an in-session code form, and
guarding it would be a loop. `?next=` is carried as a string and validated only at use.

The security model is an input to the redesign, not a target: the refresh cookie is
HttpOnly and host-only, and no visual requirement may move it into JavaScript's reach.

### 3.5 Lobby and game room

The lobby (`/play`) is a single `max-w-2xl` column: queue form, waiting card, and the
match offer surface which is global (`AppShell` mounts `MatchOfferSurface` on every
authenticated page, so an accepted challenge reaches a player wherever they are).

The **game room** is the strongest surface in the app and still the one that needs the
most design work.

What is already right, and must not be lost:

- **The board is genuinely accessible.** `role="grid"` with `role="gridcell"` buttons,
  arrow-key movement over a roving `tabIndex`, `aria-pressed` for selection, and an
  `aria-label` per square naming the square, the piece and its rank. This is better
  than most board games on the web and it is a hard thing to rebuild — the redesign
  changes its skin, not its semantics.
- **Board first in the DOM**, one `lg:` breakpoint moves the panel beside it.
- **Status is textual, not colour-only.** `StatusLine` is a `role="status"` live region
  carrying whose turn it is, connection state and rejections.
- **Clocks use `tabular-nums`** so digits do not shift the layout.
- **App updates are held** while a game is live (`useHoldAppUpdate`), because a
  service-worker reload mid-game is a running clock with no board.

What is missing:

- **No low-time state.** `ClockFace` has exactly two renderings — active
  (`border-primary bg-primary/5`) and inactive. A `bullet_1_0` game is sixty seconds
  long and nothing changes as it runs out.
- **The active-clock signal is very weak.** A 5%-opacity tint of a neutral primary is
  close to invisible; whose turn it is is carried mostly by the status text.
- **On a phone the clocks are below the board.** Board is full-width first, panel
  follows — so in a timed game the clock can be below the fold on the surface where it
  matters most.
- **No move animation, no result reveal.** Motion across the whole app is one
  `transition-colors`, one `animate-spin`, one `animate-pulse` and Radix's dialog
  `animate-in`/`animate-out`.

### 3.6 Tournaments and the bracket

The bracket (`apps/web/src/features/tournament/ui/bracket-view.tsx`) renders **rounds as columns of
equal cards in a horizontal scroller**. It has no connectors, and that is documented as
a deliberate trade: connectors imply absolute positioning and fixed row heights, and
fixed heights are what stop a bracket reflowing at 360px. The relationship is carried
by the round heading and the seed number instead, which is also the only form a screen
reader can use.

The trade is defensible and the result is still not a bracket: a reader cannot see
which two nodes feed the one above them.

**The decisive finding is that the relationship is already authoritative and already on
the wire.** `apps/api/app/modules/tournament/domain/bracket_plan.py` states it:

- a node is `(round_number, slot)`;
- its parent is `(round_number + 1, slot // 2)` — `BracketSlot.parent()`;
- its children are `(round_number - 1, slot * 2)` and `(…, slot * 2 + 1)`;
- **even slots feed the parent's light seat, odd ones the dark** —
  `BracketSlot.takes_light_seat_of_parent()`.

`BracketNodeResponse` already carries `round_number` and `slot` on every node. So a
future bracket can draw every edge as a function of the domain relationship, with
**no backend contract change and no CSS guesswork**. That is the requirement A64-025.8
inherits: the visual edge is derived from `parent()`, or it is not drawn.

### 3.7 Responsive and mobile

The strategy is consistent and thin: `sm:` and `lg:`, no `md:` cascade, no fixed pixel
widths. Real safe-area handling exists
(`pb-[max(1.5rem,env(safe-area-inset-bottom))]`). The page body never scrolls
sideways — the bracket's scroller is the only horizontal scroll in the app, and it is
labelled and focusable.

The gap is that **nothing has been designed *for* a phone**; it has been made to fit
one. The header row (§3.3) and the game room's clock placement (§3.5) are the two
places where that shows.

### 3.8 Accessibility

Strong foundations, unevenly finished.

| Present | Evidence |
| --- | --- |
| Landmarks and skip link | `AppShell` — `<header>`, `<main id="main" tabIndex={-1}>`, `<footer>`, skip link as first focusable element |
| Board keyboard/SR support | §3.5 |
| Live regions | `role="status"` in 23 files, `role="alert"` in 24 — present everywhere they are needed |
| Focusable scroll region | Bracket scroller has `tabIndex={0}` and `role="region"` |
| Dialog semantics | Radix Dialog — focus trap and focus return come with it |

| Missing | Consequence |
| --- | --- |
| `aria-current` in navigation | Nothing announces which section is open |
| `prefers-reduced-motion` handling | Only a comment saying there is nothing to reduce — true today, false the moment the redesign adds motion |
| A shared status/empty/error component | seventy-four hand-rolled live regions is seventy-four chances to omit one |

### 3.9 Loading, error, empty and success states

All four exist across the app and none is shared. There is no `EmptyState`, no
`Notice`, no `ErrorNotice` in `shared/ui` — every page writes its own, seventy-four
times over. `apps/admin`
*does* have `ErrorNotice` and `PageHeader`; `apps/web`, the larger app, does not.

### 3.10 Email

Three senders, three different levels of finish, no shared layout.

| Email | Trigger | HTML | Text | uz/ru/en | CTA |
| --- | --- | --- | --- | --- | --- |
| Verification code | Registration, resend | Yes | Yes | **Yes** | Code, not a link |
| Notification digest | `notifications` email delivery | Yes | Yes | **Yes** | Yes, styled button |
| Password reset | Forgot-password | **No** | Yes | **No — English only** | Bare URL |

Every message is hand-built as an inline-styled string at its call site. Inline styles
are correct for email — a `<style>` block is stripped by several major clients — but
there is no shared layout, no header or logo, no footer, and the brand values
(`color:#111`, `background:#111`) are duplicated literals in two files. The
password-reset message, which is the one a worried person reads, is the least finished
of the three.

A single Arena64 email layout is worth building (A64-025.10). It is a small, contained
piece of work with an obvious shared shape.

### 3.11 Dependencies

The stack is already the right one and the redesign needs nothing new.

| Need | Already present |
| --- | --- |
| Styling | Tailwind v4 (CSS-first, `@theme inline`) |
| Primitives | Radix — `react-dialog`, `react-avatar`, `react-slot` |
| Variants | `class-variance-authority`, `clsx`, `tailwind-merge` |
| Icons | `lucide-react` |
| Forms | `react-hook-form` + `zod` + `@hookform/resolvers` |
| Routing | `@tanstack/react-router` |
| Data | `@tanstack/react-query`, `axios` |
| Animation | `tw-animate-css` |

`apps/web/components.json` is configured for shadcn/ui with `@/shared/ui` as the component
alias, so the missing primitives (§3.2) can be **generated into the existing theme**
rather than hand-written or imported from a new library. Every primitive the design
system needs is reachable with zero new dependencies.

### 3.12 UX-affecting performance

Only findings a player could feel are recorded; micro-optimisation is out of scope.

- **Route-level code splitting is already in place** — every route is a
  `lazyRouteComponent`.
- **`apps/web/src/features/auth/model/session-provider.tsx` is 362 lines** and sits above every route. It is the single
  largest component in the app and the one whose re-renders reach everything.
- **`apps/web/src/features/game/ui/game-panel.tsx` (314 lines) and `…/ui/board.tsx` (266)** re-render on every clock tick
  (250 ms). The clock is `tabular-nums` text, so this is cheap today; it is the place
  to look first if the game room ever feels slow.
- No duplicated fetches or waterfalls were found at the query layer — React Query keys
  are centralised per feature in `apps/web/src/features/*/api/keys.ts`.

---

## 4. Findings by priority

### P0 — the product is not usable as presented

**P0-1 — `/` is a developer exhibit.**
`apps/web/src/pages/home/index.tsx` is the landing route. It renders a heading, the sentence
*"Application shell. No gameplay surface is built yet."*, a card demonstrating
`Skeleton` and `Spinner`, and a `FormDemo` from `apps/web/src/features/form-demo`. It is
untranslated — the only page in the app that is, because every other string in the
product goes through `t()` and there are 775 keys in each of uz, ru and en.

Its own docstring records the intent: *"The lobby that eventually lives at `/` is
A64-020.5's, and replacing this file is that phase's first commit."* A64-020.5 built
`/play` and never replaced it.

The consequence is that the first thing any visitor sees — including a signed-in player
who clicks nothing — is the product telling them it does not exist yet. Nothing links
to `/play` from it.

*Affected surface:* landing, first-run experience, and every share of the root URL.
**Fixed by A64-025.3** — see §9.1. `src/features/form-demo` was deleted with it.

### P1 — a primary flow is seriously degraded

**P1-1 — The header does not fit a phone.**
`session-menu` renders Play, Tournaments, Friends, Profile-with-avatar and Sign out as
five buttons, and `app-shell` puts them in a `h-14` row beside the brand,
`NotificationBell` and `ThemeToggle`. The only responsive rule is
`hidden … sm:inline` on the user's name. At 360px this is eight interactive elements
and a wordmark in one row, with Uzbek labels ("O'ynash", "Turnirlar", "Do'stlar").
**Fixed by A64-025.3** — see §9.4, measured at 360, 768 and 1280.

**P1-2 — The clock has no low-time state.**
`ClockFace` renders identically at 60 s and at 2 s. Arena64 offers `bullet_1_0`. A
player's most time-critical information has no escalation, and the active-clock cue is
a 5 % tint of a neutral primary.
*Future task:* A64-025.6.

**P1-3 — On a phone the clocks sit below the board.**
The game page stacks board-then-panel until `lg:`. On the surface where time matters
most, the clock can be off-screen.
*Future task:* A64-025.6.

### P2 — a noticeable UX problem

| # | Finding | Task |
| --- | --- | --- |
| ~~P2-1~~ | ~~Bracket has no visual parent-child relationship, though the relationship is authoritative and already on the wire (§3.6)~~ | **Fixed** — §16 |
| P2-2 | Password-reset email is English-only and plain-text-only while the other two are trilingual HTML (§3.10) | A64-025.10 |
| P2-3 | No shared empty/error/loading component — 74 hand-rolled live regions (§3.9) | **Foundation laid** — `ListState` promoted and `Notice` added (§10.7); the sweep across surfaces is A64-025.11/.12 |
| ~~P2-4~~ | ~~No `aria-current` anywhere in navigation~~ | **Fixed** — §9.3 |
| ~~P2-5~~ | ~~No brand colour, no semantic colours~~ | **Fixed** — §10.1, §10.3 |
| ~~P2-6~~ | ~~`/games/history` had no navigation entry~~ | **Fixed** — §9.2. `/challenges` and `/search` stay inside the Social section, which is where they belong |

### P3 — polish and consistency

| # | Finding | Task |
| --- | --- | --- |
| ~~P3-1~~ | ~~Brand wordmark is a `<span>`~~ | **Fixed** — §9.4 |
| ~~P3-2~~ | ~~Hardcoded English in the shell~~ | **Fixed** — the skip link and the theme group are localised |
| ~~P3-3~~ | ~~`session-menu` is misnamed~~ | **Fixed** — it is `AccountMenu` and holds only the account, §9.5 |
| ~~P3-4~~ | ~~`form-demo` ships in the production bundle~~ | **Fixed** — deleted |
| P3-5 | No `prefers-reduced-motion` handling — correct today, wrong once motion is added | A64-025.12 |
| P3-6 | Eight primitives; Badge, Select, Tabs, Tooltip, Dropdown, Switch re-authored per feature | A64-025.2 |

**Nothing was fixed in A64-025.1.** P0-1 is a one-line route change to make and a
product decision to get right — what the landing page *should* be for an anonymous
visitor is A64-025.3's subject, and shipping a guess here would be the scope creep this
task's brief forbids.

---

## 5. Design principles

Twelve principles, each argued from something in §3 rather than from taste.

1. **The board is the product.** Every layout decision in the game room is judged by
   what it does to the board's size and clarity first. Nothing decorative may take
   space from it.
2. **State is text before it is colour.** The existing `StatusLine` and the bracket's
   worded node states are right, and they are why the app is usable without colour
   today. Colour reinforces; it never carries alone.
3. **Time is loud.** A clock running out is the most urgent thing the product ever has
   to say. It must change appearance, not merely value (P1-2).
4. **Mobile is a design target, not a fallback.** Nothing ships as "it fits at 360px";
   surfaces are designed at 360px and allowed to expand.
5. **Accessibility is a property of the component, not a mode.** The board's
   `role="grid"` and roving tabindex are the standard the rest of the app is held to —
   no separate accessible variant, ever.
6. **One way to say each thing.** An error looks the same everywhere, a status looks
   the same everywhere, an empty list looks the same everywhere — enforced by a shared
   component, not by discipline (P2-3).
7. **A relationship the domain knows is a relationship the UI draws.** The bracket's
   edges come from `BracketSlot.parent()`. No CSS approximation of a data structure.
8. **Motion earns its place.** Fast, functional, interruptible, and disabled under
   `prefers-reduced-motion`. Nothing animates because it can.
9. **Brand is a colour, not a texture.** Arena64 needs one identity colour and a small
   set of semantic ones. It does not need gradients, glass or illustration.
10. **Never trade a security or privacy semantic for a visual one.** The HttpOnly
    session, the verified-email gate and the privacy field rules constrain the design
    and are not negotiable by it.
11. **Extend the stack before adding to it.** `apps/web/components.json` already points shadcn
    at `@/shared/ui`. A new dependency needs a demonstrated gap, not a redesign as its
    justification.
12. **Density without noise.** Arena64 shows ratings, clocks, seeds and standings.
    Numbers get `tabular-nums` and hierarchy; they do not get boxes around every one.

---

## 6. Design-system direction

Blueprint only — nothing below is built.

**Foundations.** Keep the existing OKLCH token file and extend it rather than replace
it: adding a brand hue and semantic game tokens (`--win`, `--loss`, `--draw`,
`--turn`, `--time-low`, `--live`) to `:root` and `.dark`, exposed through
`@theme inline` exactly as the current tokens are. Radius, spacing, breakpoints and
typography stay as they are — none of them was found wanting.

**Primitives to add** (`apps/web/src/shared/ui/`), all generatable via shadcn into the existing
theme: Badge, Select, Switch, Tabs, Tooltip, DropdownMenu, Notice, EmptyState,
IconButton. Existing: Button, Input, Card, Dialog, Avatar, Skeleton, Spinner,
ErrorBoundary.

**Product components**, each replacing something currently inline: `PlayerCard`,
`RatingDisplay`, `GameClock` (absorbing `ClockFace` and owning the low-time state),
`MatchResult`, `TournamentCard`, `BracketMatch` (owning the derived edge),
`NotificationItem`, `EmptyState`.

Overlap to respect rather than rebuild: `widgets/player-row`, `widgets/rating-cards`,
`widgets/profile-header`, `widgets/match-offer`, `widgets/statistics-panel` already
exist and largely hold the right shapes.

---

## 7. Roadmap

Ordered by dependency, not by surface importance. The audit moved the app shell ahead
of the design system: P0-1 and P1-1 both live there, and neither should wait for
tokens.

| Task | Objective | Depends on | Non-goals |
| --- | --- | --- | --- |
| **A64-025.1** | Audit and foundation — this document | — | Any implementation |
| **A64-025.3** | App shell, landing page, navigation. Fixes P0-1, P1-1, P2-4, P2-6, P3-1…P3-4 | .1 | New features; visual restyle beyond what the fixes need |
| **A64-025.2** | Design-system foundation: brand and semantic tokens, missing primitives, shared state components | .1 | Rewriting surfaces; new dependencies |
| **A64-025.4** | Authentication UX | .2 | Changing the session or verification model |
| **A64-025.5** | Lobby and matchmaking | .2 | Changing the queue state machine |
| **A64-025.6** | Game room. Fixes P1-2, P1-3 | .2 | Changing board semantics or the protocol |
| **A64-025.6A** | Game room visual hardening — §13.10 | .6 | New data on the board |
| **A64-025.6B** | Seat ratings on the snapshot — §14 | .6A | Reading a rating per player |
| **A64-025.6C** | The board itself, and the panel around it — §15 | .6B | New data on the board; changing board semantics |
| **A64-025.7** | Tournament and bracket, edges derived from `BracketSlot.parent()`. Closes OQ-4 | .2 | Backend contract changes; canvas, zoom or drag |
| **A64-025.8** | Friends and social | .2 | Changing privacy or blocking rules |
| **A64-025.9** | Profile and player | .2 | Changing privacy rules; inventing a statistic |
| **A64-025.9B** | Home, and the account menu in the header | .2, .3 | Inventing a statistic the API does not return |
| **A64-025.10** | Notifications | .2 | Admin notification surfaces |
| **A64-025.10E** | Email design system. Fixes P2-2 | .2 (tokens only) | New email types |
| **A64-025.11** | Global UI consistency and component cleanup | .3–.10 | Re-architecting layouts already designed mobile-first |
| **A64-025.12** | Motion and interaction system. Fixes P3-5 | .3–.10 | Adding motion for its own sake |
| **A64-025.13** | Closing audit | all | New work |

**Renumbered on 2026-09-03**, at the owner's direction, and the order below is
theirs: tournament before social, social before profile, notifications after both.
The plan this replaces read `.7` profile and social, `.8` tournament, `.9`
notifications, `.10` email.

Two consequences worth stating rather than leaving to be noticed. The game-room
tasks that actually shipped — `.6A` and `.6B` — were never in this table and are
now, because a plan that omits half the delivered work cannot be checked against
it. And the email design system keeps its scope under `.10E` rather than a number
of its own: it is the email half of notifications, it was absent from the new
ordering, and P2-2 is a live defect — the password-reset mail is English-only
plain text while the other two are trilingual HTML.

---

## 8. Open questions

| # | Question | Blocks |
| --- | --- | --- |
| ~~OQ-1~~ | ~~What does `/` show an anonymous visitor?~~ | **Closed by A64-025.3** — `/` is the authenticated player's product home; the route keeps its existing lack of a guard, so an anonymous visitor gets a signed-out home offering sign-in and registration. A public marketing page is a separate surface for a separate audience and is not this epic's |
| ~~OQ-2~~ | ~~What is Arena64's brand colour?~~ | **Closed by A64-025.2** — indigo, `oklch(0.5 0.19 275)` light and `oklch(0.68 0.16 275)` dark. The reasoning is in §10.1 and it is reversible in two token values |
| ~~OQ-3~~ | ~~Where do the clocks go on a phone?~~ | **Closed by A64-025.6** — neither. Each clock lives in its player's seat, and the seats sit directly above and below the board at every width. §13.3 |
| ~~OQ-4~~ | ~~Does the bracket keep horizontal scroll on mobile, or switch to a round-at-a-time view below `sm:`?~~ | **Closed by A64-025.7** — horizontal scroll, kept. A segmented view hides the comparison the bracket exists to make, and the edges are now drawn, which is what the scroll was missing rather than the scroll being the problem. §16 |

---

## 9. The navigation model — A64-025.3

### 9.1 `/` is the product home

The authenticated player's home, not a marketing page. The route keeps the
guard it always had — none — so an anonymous visitor is not redirected; they
get a signed-out home whose only offers are sign-in and registration. That
closes OQ-1 and leaves a public landing page as a separate future surface,
because it is written for a different reader and nobody has written its copy.

The page issues **no request**. Every word comes from the session already in
memory and every destination is a route that already exists, so there is no
loading and no error state to design. §3 of A64-025.3 forbids inventing a
dashboard — an online count, a streak, a recommended tournament — and the
reason is that a plausible number the server never sent is worse than an
empty page.

What it renders, signed in: a greeting, one large `Play` call to action, and
four cards — Tournaments, Challenges, Friends, Match history. Signed out: the
product name and the two authentication actions, and nothing else, because
every other destination is behind the verified-email guard and a link that
redirects to sign-in is a link that lies.

### 9.2 Four sections

| Section | Link | Owns | Answers |
| --- | --- | --- | --- |
| Play | `/play` | `/play`, `/games/$matchId`, `/games/$matchId/replay` | Where do I start a game? |
| Tournaments | `/tournaments` | `/tournaments`, `/tournaments/$tournamentId` | Where are the tournaments? |
| Social | `/friends` | `/friends`, `/friends/requests`, `/friends/blocked`, `/challenges`, `/search`, `/players/$username` | Where are my friends and challenges? |
| History | `/games/history` | `/games/history` | Where are my past games? |

Four, not twenty-five. A section owns routes its link does not point at:
`SocialNav` already carried requests, challenges, blocked and search, so
`/search` is not a top-level destination — it is where you look for a person,
which is what that whole section is about.

`/games/history` had no navigation entry anywhere and was reachable only from
`/profile`. It has one now.

### 9.3 Active route

`useActiveSection` reads **the route ids the router matched**
(`useRouterState(state => state.matches.map(m => m.routeId))`) and asks which
section owns one of them. Not a pathname comparison, and not
`matchRoute({ to: "/games/$matchId" })` — that was tried and is wrong,
because it answers "could this pattern match" and `$matchId` swallows the
literal `history`. The matched ids answer "what did match", which is the
question, and the router has already ranked a static segment above a
parameter.

The ids are typed from `RouteIds<RegisteredRouter["routeTree"]>`, so a
section claiming a route that does not exist is a compile error rather than a
section that silently never highlights.

The current link carries `aria-current="page"` and a weight change, never
colour alone. TanStack's own automatic `aria-current` is not relied on: a
section is current for routes its link does not point at, so the attribute is
set from `useActiveSection` and is the only source.

### 9.4 Desktop and mobile shells

Desktop: wordmark, the four sections, then the bell, the account controls and
the theme group at the far end. Three named groups — `Brand`,
`PrimaryNav`, `AccountMenu` — because when they were one component nobody
adding a section knew where to put it.

Below `md` the sections move into a panel behind a menu trigger, and the
header is a trigger, the wordmark, the bell, the avatar and the theme group.
The panel is the existing Radix `Dialog` with a sheet's geometry applied as a
`className`, which brings the focus trap, focus return, `Escape` and the
trigger's `aria-expanded` without a new dependency. It closes on navigation.

Measured in Chromium at 360, 768 and 1280: **zero horizontal overflow** on
the page and in the header, signed in and signed out.

### 9.5 The account boundary

`SessionMenu` became `AccountMenu` and lost the product. What is left is the
account: the avatar and name linking to `/profile` — from which Settings is
reached — and sign-out. On a phone those two plus Settings live in the panel
under their own `Account` landmark, so the panel does not present the account
as a fifth product section.

Sign-out semantics are unchanged, including that a failed call has already
cleared the device before it throws.

### 9.6 Deferred on purpose

Visual-token work is A64-025.2's and none of it happened here: no brand
colour, no new palette, no typography or radius change. The header uses the
tokens that already existed.

Two things measured here are recorded rather than fixed, because they belong
to the primitive layer:

- ~~the theme group's three buttons are 36px tall~~ — **fixed by A64-025.2**,
  §10.5: the floor moved into the primitive;
- the English theme labels are now `Light`, `Dark`, `System` rather than
  `Light theme` — the translations that already existed are shorter than the
  hardcoded English they replaced. They sit inside a group named "Toggle
  theme", which carries the context, but a longer accessible name would be
  better and needs new keys.

---

## 10. The design system — A64-025.2

A64-025.1 found the foundation in better shape than a redesign brief
assumes: a complete OKLCH token file, light and dark, almost no arbitrary
values, and only three colour utilities used across the whole tree. Nothing
here replaces that. This extends it.

### 10.1 Brand — indigo

`--primary: oklch(0.5 0.19 275)` in light, `oklch(0.68 0.16 275)` in dark.
That closes OQ-2.

Chosen from the repository rather than from taste, and the constraint that
decided it is on the board: `board.tsx` tints the last move with
`color-mix(… var(--color-primary) 18% …)` and draws every legal-move dot
with `bg-primary`. The brand is therefore not only a button colour — it is
the colour a player reads the position through, over neutral squares and
beside near-white and near-black pieces.

| Candidate | Verdict |
| --- | --- |
| Electric blue ~255° | White text at 4.69:1 — passes AA and only just. It is also the default tech-product blue and the closest hue to the game's best-known competitor, so it costs distinctiveness and buys nothing |
| **Indigo ~275°** | **Chosen.** White text at 6.12:1 light, 6.52:1 dark. Far from `--destructive` (~27°) and from `--success` (~150°) on the hue wheel, so a status colour can never be mistaken for the brand. Cool enough to separate cleanly from a neutral board |
| Violet ~295° | Works numerically (5.76:1) and reads more consumer-social than competitive |
| Amber / gold | Rejected on measurement: white on amber cannot reach 4.5:1 without becoming brown, and it collides with light board squares |

Every ratio above was computed from the OKLCH values rather than estimated.

### 10.2 Light and dark

Both themes define every token. The dark values are not the light ones
dimmed: `--primary` goes *lighter* and slightly less saturated in dark
(0.68/0.16 against 0.50/0.19), which is what keeps it legible on a
near-black page — 6.65:1 against the background.

`--ring` is now the brand in both themes. A focus ring that is grey reads as
a border; one that is the product's own colour reads as deliberate.

### 10.3 Game and status semantics

Two tokens added, and the restraint matters as much as the additions:

| Token | Why it exists | Callers today |
| --- | --- | --- |
| `--success` / `--success-foreground` | A rating gain and an online dot were `text-emerald-600` and `bg-emerald-500` — fixed palette values that do not change with the theme, in an app where everything else does | rating delta, two presence dots |
| `--warning` / `--warning-foreground` | The only one of `Notice`'s four tones with no existing token. Its foreground is *dark*, because white on amber never reaches 4.5:1 | `Notice` |

Deliberately **not** added:

- **loss** — `--destructive` already means it, and a second red is two things
  to keep in step;
- **draw / neutral result** — `--muted-foreground`, and the history row
  already carries the result in words;
- **active turn** — `--primary` now has a hue, which is what that signal was
  missing;
- **low time** — it has no caller. A64-025.6 builds the clock and will add
  it with the component that needs it, which is the point at which its value
  can be chosen against a real background.

### 10.4 Typography, spacing, radius, elevation

Unchanged, and that is the finding rather than an omission. The audit found
one type scale in consistent use, `--radius` already deriving four sizes,
and shadows used only where shadcn's own components use them. Adding a
parallel scale would give the codebase two.

The conventions the existing code already follows, written down so the next
task does not re-derive them:

| Role | Pattern |
| --- | --- |
| Page title | `text-2xl font-semibold tracking-tight`, one `h1` per route |
| Section heading | `text-lg font-semibold tracking-tight` |
| Card heading | `text-base font-medium` |
| Body | inherited; `text-sm` in dense surfaces |
| Secondary | `text-muted-foreground text-sm` |
| Meta | `text-muted-foreground text-xs` |
| Numeric | **`tabular-nums`**, always — a clock or a rating whose digits change width makes the layout twitch once a second |
| Page gap | `gap-6` to `gap-8` |
| Section gap | `gap-4` |
| Card padding | shadcn `Card`'s own |
| Compact row | `gap-2`, `min-h-11` |

### 10.5 Control size and the touch target

**Every player-facing control is at least 44px tall, and the primitive
guarantees it.**

It did not. `min-h-11` appeared **112 times** across `apps/web`, on 86 of
the 100 `<Button>`s, because the stock shadcn sizes are 32–40px and this
product's standard is 44. A rule pasted a hundred times is a rule that will
be missed on the hundred-and-first — and the fourteen that missed it are how
A64-025.3 came to measure a 36px control in the app shell.

`min-h-11` is now on `buttonVariants`' base, so `size` chooses padding and
type while the floor is unconditional; `size="icon"` is `size-11`, because a
square control must grow in both directions or stop being square. Measured
in Chromium after the change: the smallest interactive control on `/` is
44px at 360 and 1280, in both themes.

The hundred-odd now-redundant `min-h-11` classes are harmless and are left
in place: removing them is a hundred-file sweep, and a foundation change is
the wrong commit to hide one in.

### 10.6 Focus and interaction

Unchanged apart from the ring's colour. The base already carries
`focus-visible:ring-[3px]`, `disabled:opacity-50`,
`aria-invalid:border-destructive` and a hover for every variant. No state is
carried by hover alone.

### 10.7 Feedback primitives

`ListState` was **promoted, not invented**. It already existed in
`features/social` and five social pages used it, while tournaments, history
and notifications each rewrote the same three branches. The barrier was not
the component — it was that its strings were `social.state.*`, so nothing
outside that feature could reuse it without announcing "social" to somebody
reading a tournament list. The strings are now `state.*` and the file is in
`shared/ui`. Its markup is untouched.

`Notice` is new: one short message, four tones, and **the tone chooses the
role** — `error` is `role="alert"`, everything else is `role="status"`. A
success that interrupts a screen reader mid-sentence is worse than one
nobody hears. A caller may override `role` for the case the rule does not
fit. Adopted once, on the match-history failure, to prove it against a real
surface; the other seventy-odd hand-rolled regions are A64-025.11/.12's.

`emptyTitle` and `emptyHint` stay with the caller. "No tournaments open" is a
domain sentence, and a generic primitive that owned it would be `shared/ui`
holding five features' vocabulary.

### 10.8 What was deliberately not built

| Candidate | Decision |
| --- | --- |
| `Badge` | **Not built.** One true status pill exists (`tournament-card`); the other `rounded-full` uses are a filter chip, an avatar, a count bubble and a board piece. CLAUDE.md §2.7 earns an abstraction on the third case, not the first |
| `Sheet` | **Not built.** One consumer — `MobileNav`, which applies a sheet's geometry as a `className` on the existing `Dialog`. A second consumer makes it worth extracting |
| `Typography` components | **Not built.** A component per heading level is a system nobody reads; §10.4 is the contract instead |
| Storybook or a gallery route | **Not built**, and not by omission: A64-025.3 removed a developer surface from `/`, and adding one back would undo it. The primitives are proved by tests |
| Motion tokens | **Not added.** The policy below needs none yet |

### 10.9 Motion

The policy, ahead of any motion existing: functional, short, interruptible,
and silent under `prefers-reduced-motion`. Nothing may animate that delays a
player's input, and nothing on the board animates before A64-025.6 owns it.
No animation library — `tw-animate-css` and CSS transitions are already
here and already enough.

### 10.10 Primitive inventory

Ten: Avatar, Button, Card, Dialog, ErrorBoundary, Input, **ListState**,
**Notice**, Skeleton, Spinner. Two added, both earned.

Still absent and still unearned until a second caller appears: Select,
Switch, Tabs, Tooltip, DropdownMenu, Badge, Sheet. Each is a `shadcn add`
away into this theme when the caller exists — `apps/web/components.json`
already points at `@/shared/ui`.

---

## 11. Authentication — A64-025.4

### 11.1 What was already right

The audit found the *semantics* of authentication in better shape than any
other surface, and none of it was rewritten: `autoComplete="email"` and
`current-password`/`new-password` in the right places, `noValidate` with a
zod resolver so the messages are translatable, `disabled={isSubmitting}` on
every submit, one bounded message for a rejected credential so the client
cannot reintroduce the enumeration oracle the API avoids, `safeRedirect`
between `?next=` and a navigation, and `FormField`/`FormError` already
generating the four ids that have to agree.

What was wrong was the composition. A `max-w-sm` card in the middle of a
very wide page — the generic starter form, saying nothing about a
competitive board game.

### 11.2 The shell

One composed surface instead of a floating card. Above `lg` it splits into
an identity panel and the form; below `lg` the panel is gone and the form is
the page, with the wordmark above it. The same DOM, one breakpoint, no
second layout.

The panel sits on `--primary` with an 8×8 grid over it — one
`repeating-conic-gradient` of white at seven per cent, so it follows the
brand into dark mode with no second value to keep in step. No image, no
illustration, no dependency, and `aria-hidden`, because the sentence beside
it already says what it means. §17's list of things not to build — hero,
carousel, particles, counter — is respected; an eight-by-eight grid is not
decoration on a draughts product, it is the subject.

### 11.3 Password visibility

Four of the five auth forms ask for a password and none let you check it.
`PasswordField` wraps `FormField` rather than replacing it, so the label,
the description and the error stay wired to one control and the id
generation stays in one place.

The toggle is `type="button"` (a bare button in a form submits it), changes
only the input's `type` so the value and the caret survive, carries
`aria-pressed` and a name that says what pressing it will do, and leaves
`autoComplete` to the caller — `current-password` and `new-password` are a
distinction password managers act on.

### 11.4 Feedback

`FormError` and `FormStatus` predate A64-025.2's `Notice` and had their own
class strings for the same four tones. They now render `Notice`, so a
failure looks the same in a form as anywhere else. `FormError` keeps the one
thing `Notice` does not do — moving focus to the message, which is what
makes WCAG 2.1 §3.3.1's "identify the error" actually reach a screen-reader
user.

`FormStatus` gained a `success` tone. A verified address and a changed
password are the end of a journey; "we sent the link" is a step. They looked
identical, and a person stops reading a signal that never varies.

### 11.5 Control size, corrected

A64-025.2 put the 44px floor on `Button` and left `Input` at 36 — the taller
half of the pair a person actually taps on a phone. Measured at 36px on
`/login` at 360px, now `h-11`. The standalone "Forgot your password?" link
got the same floor; "Create one" did not, because it is a link inside a
sentence and WCAG 2.5.8 exempts those.

### 11.6 Measured

Chromium, all four auth routes:

| Route | 360 light | 360 dark | 1280 light | 1280 dark |
| --- | --- | --- | --- | --- |
| `/login` | 0 overflow, 44px | 0, 44px | 0, 44px | 0, 44px |
| `/register` | 0, 44px | — | — | 0, 44px |
| `/verify-email` | 0, 44px | — | — | 0, 44px |
| `/forgot-password` | 0, 44px | — | — | 0, 44px |
| `/reset-password` | 0 overflow | — | — | 0 overflow |

Exactly one `h1` per route in every combination, and no clipped content.

### 11.7 Unchanged on purpose

Nothing in the security model moved: the HttpOnly refresh cookie, token
storage, refresh and revocation semantics, the verified-email guard,
`safeRedirect`'s open-redirect protection, the single message for a rejected
credential, and the route guards. `/`'s semantics from A64-025.3 are
untouched, and no backend contract was read differently or extended.

`/verify-email` keeps its two-mode behaviour — a mailed `?token=` and an
in-session code form behind one path — and its lack of a guard, which is
deliberate and documented in the route tree.

---

## 12. Lobby and play — A64-025.5

### 12.1 The state machine, unchanged

`useLobbyState` derives everything from two authoritative reads and stores
nothing:

    bootstrapping → idle → queued → match_offer → awaiting_opponent
                                  ↘ transitioning → the board
                    unavailable (both reads failed)

Four *busy* states — joining, accepting, declining, transitioning — are the
page's, because they describe a request the cache cannot see. A pending
match outranks a ticket, always, and the precedence is applied in one place.
None of that moved: this task changed how the states look, not what they
are.

Two decisions start a game: mode and time control. Variant is not offered
because `ProductVariant` has one member, and region is not offered because
every non-default value shrinks the pool on a single-region deployment.
Mode defaults to casual; the clock deliberately has no default, because
every control is a different game.

### 12.2 What was weak

Not the semantics — the composition. A single `max-w-2xl` column with two
fieldsets and, below both of them, the only button a player came to press.
On a phone that button was under a fold. Selection was a one-pixel border
and a five-per-cent tint of a colour that had no hue until A64-025.2. The
queued state replaced the whole page with a spinner over a four-row table.

### 12.3 The action follows the viewport

The submit is now a **sticky bar** at the bottom of the form: an ordinary
row at the end on a wide screen, pinned to the viewport on a phone, with
`env(safe-area-inset-bottom)` so an iPhone's home indicator does not cover
it. One rule, no second layout.

It also says what it will do — `3+2 · Casual` — read from the catalogue
rather than stored, because the catalogue is the authority and a second copy
of the label is a second thing to keep in step. Before a clock is chosen it
says which choice is missing instead.

Measured in Chromium: the call to action is fully visible without scrolling
at 360px in both themes.

**Post-implementation visual review (A64-025.5a).** The first version used
one rule for both widths, and a screenshot showed why that was wrong:
`bg-background` inside a `bg-card` surface is *darker* than its parent in
dark mode — `--background` is 0.145 and `--card` is 0.205 — so the bar read
as a black slab bolted onto the card rather than its last row. Above `sm`
there is now no surface at all: the action is the final row of the form,
aligned right, on the card it already sits on. Below `sm` the bar stays,
because putting the button in reach of a thumb was the point, and it takes
`bg-card` — the colour of the thing it is the bottom of.

The same review removed the "choose a time control" line. The fieldset above
already says it, and repeating it in the action area made the emptiest state
the loudest thing on the page; the reason the button is disabled now reaches
a screen reader through `aria-describedby` instead. The summary appears only
once there is something to summarise.

The action was also renamed. "Join the queue" is what the API is called;
"Find an opponent" is what the player is doing, and the queue is an
implementation detail they did not ask about.

### 12.4 Choosing a clock

The time control is what decides the game, so it is what is legible: the
clock is set large and `tabular-nums` (so `1+0` and `10+0` do not shuffle
the grid) with the speed class beneath it. The chosen card takes a brand
border, a brand tint and a brand ring; unchosen cards respond to hover.

The control is still a native `input type="radio"` inside a
`fieldset`/`legend` — the browser's arrow-key behaviour and the screen
reader's "3+2, radio button 2 of 4" are exactly right and were not
re-implemented. The visual change is entirely on the label around it.

### 12.5 Queueing

The searching state is now the page rather than a line above a table: a
pulse on the brand, the status in words, the elapsed time under it, and the
configuration as four labelled chips. The pulse is two CSS rings with
`motion-reduce:animate-none`; it carries no information, because the words
beside it do and `role="status"` is what announces them.

Cancel is secondary and has no confirmation — leaving a queue costs nothing
and is instantly repeatable, so a dialog would protect against no
consequence. The guard that matters is `disabled` while the request is in
flight, which was already there.

### 12.6 The offer

Same shared surface, redesigned rather than duplicated — `AppShell` still
owns the one instance so a player paired while reading a profile still sees
it. The opponent leads now, with a larger avatar and name; the configuration
uses the same chips the queue does, so a player recognises what they were
waiting for; and the countdown is its own bordered strip with the number at
`text-3xl`, tinted with `--warning` under ten seconds while the words beside
it stay the same.

Everything that made it correct is untouched: `role="alertdialog"`, escape
and outside-click prevented, `aria-live="off"` on the number with a separate
polite region that speaks only at 30, 20, 10 and 5, and both buttons named
with the opponent.

### 12.7 Playing a friend

A named, linked second path to `/challenges` — a deep link, not a second
implementation of challenge creation. Below the queue, because a random
opponent is the faster path and the one this page is for. Hidden while
queued, where it would be a way to leave a search a player just started.

### 12.8 No invented data

There is no wait estimate, no queue position, no online count, no opponent
preview and no recommended mode. The backend publishes none of them, and a
plausible number the server never sent is worse than an empty space. What
the queue shows — mode, clock, speed class, entry time, elapsed — is all
from the ticket.

### 12.9 Verified

Chromium, signed in, one account per measurement:

| State | 360 light | 360 dark | 1280 light | 1280 dark |
| --- | --- | --- | --- | --- |
| Idle | 0 overflow, 44px, CTA in view | same | same | same |
| Queued | 0 overflow, 44px, `role="status"` | — | — | 0 overflow, 44px |

The offer was exercised end to end rather than measured: `play.spec.ts` runs
two real players into one pool, through the redesigned dialog, to the board.

### 12.10 Deferred on purpose

The board, the clocks, the player cards and the result screen are
A64-025.6's. Nothing here reaches past the handoff — `transitioning` still
navigates exactly as A64-022's hardening left it.

---

## 13. The game room — A64-025.6

### 13.1 What was wrong

Not the engine, the protocol or the board. `specs/product-experience.md`
§3.5 already recorded that the board is one of the better things in the
product — `role="grid"`, arrow-key movement over a roving tabindex, a real
`<button>` per square, an `aria-label` naming the piece — and none of that
was touched.

What was wrong was that everything *around* the board lived in a side panel:
two clocks, the status line, and no player identity at all beyond the words
"Opponent" and "You". On a phone that panel is below the board, so in a
timed game the clock could be under the fold. That was OQ-3.

### 13.2 Players are real people now

`GET /api/v1/users/{user_id}` resolves a `player_id` to a name and a
picture. It is documented in the schema as existing precisely because "a
match card or a leaderboard row holds an id and needs a handle to render",
and it carries no email, no account state and no storage key. So the seats
show who is playing with **no contract change**.

Ratings were not shown here, and that deferral is **closed by A64-025.6B**
(§14): the snapshot carries the seat ratings, so the right fix was taken
rather than the second privacy-governed round trip per player that
`GET /profiles/{username}` would have cost.

### 13.3 Clock placement — OQ-3, closed

Each clock lives in its player's seat, and the seats are part of the board
column:

    opponent seat + clock
    BOARD
    own seat + clock

At every width. On a phone there is no side panel to lose a clock in; on a
desktop the seats stay attached to the board rather than sitting across the
page. The viewer is always the near seat, matching the board's orientation.

### 13.4 Active turn, in three signals

The active seat takes a brand border and tint, its digits go
`text-primary`, and the turn is stated in words underneath. Colour is
reinforcement; the sentence is the signal. The status line above the
controls still says whose move it is as a `role="status"` live region, so a
screen reader hears the change without the clock announcing every tick.

### 13.5 Low time — a presentation rule, and only that

Under **ten seconds** the active clock takes `--warning` and gains the words
"low on time". Ten because this product already uses it: A64-025.5 tinted
the match offer's countdown at the same threshold, and two definitions of
"nearly out of time" in one product is worse than an imperfect one.

Nothing about the game changes — the server is still the only thing that
flags a clock. A threshold relative to the control (the last tenth of the
base time) would be better and is **not possible today**: `ClockPayload`
carries only the remaining milliseconds and `SnapshotPayload` does not carry
the time control. Closing that gap is a contract change and is recorded here
rather than guessed at.

The warning is suppressed when the clock is not running, so a finished game
does not shout about the loser's last two seconds.

### 13.6 What did not change

The board's semantics and interaction model, the reducer, the realtime
protocol, the draw agreement state, the resign confirmation, the rating
result poll, the completion surface and the replay link. The quick-message
infrastructure already existed and was not rebuilt — the bubbles simply
moved to sit beside the seat that sent them, which is where A64-023.2 §6
always wanted them and is now literally true.

### 13.7 Deferred, with the reason

| Gap | Why |
| --- | --- |
| ~~Seat ratings~~ | **Closed by A64-025.6B** — the snapshot carries them. §14 |
| Captured / material summary | The client holds a position, not a capture history. Deriving "captured so far" from the opening position and the current one is arithmetic the domain does not publish, and a second truth source next to an authoritative board is exactly what §2 forbids |
| Control-relative low-time threshold | §13.5 — needs the time control on the snapshot |

### 13.8 Measured

A real two-player game, paired through the lobby in Chromium:

| Width | Theme | Page overflow | Board | `h1` |
| --- | --- | --- | --- | --- |
| 1280 | dark | 0 | 630px | 1 |
| 768 | light | 0 (after §13.9) | 736px | 1 |
| 360 | light | 0 | 328px | 1 |
| 360 | dark | 0 | 328px | 1 |

A board square is 41px at 360 — below the 44px control floor and bounded by
arithmetic rather than by choice: eight squares plus the page's own padding
do not fit 44px each in 360. The board is the one control whose size is the
viewport's to decide.

### 13.9 A defect this task found in the shell

Measuring the game room at 768 signed *in* found 110px of horizontal
overflow in the **header** — the wordmark, four nav sections, the bell, the
avatar, the display name and a sign-out button do not fit. A64-025.3 missed
it because it measured 768 signed *out*, where the account cluster is one
"Sign in" link.

The display name now returns at `lg` rather than `sm`, and the account
cluster keeps its tight gap until then. Measured signed in at 360, 768,
1024 and 1280: zero overflow at all four.

### 13.10 Visual hardening — A64-025.6A

A64-025.6 fixed the *composition*: the seats and clocks moved onto the
board and OQ-3 closed. A manual screenshot review then found the surfaces
around them had not been designed at all, and three things were wrong.

**Three blocks pretending to be a panel.** The status was a card, the
quick-message controls were two bare buttons floating on the page
background, and the game controls were a second card. That is what made the
right-hand side read as leftovers. They are one bordered surface now, with
dividers between the groups.

**Two actions of very different weight, drawn identically.** Offering a draw
and resigning were both `variant="outline"` side by side. The draw keeps the
neutral outline; the resignation is text on `--destructive` — unmistakable
without being a red slab a thumb finds by accident during a bullet game.

**The result was a bordered box with a bold line in it.** It is the moment
the game is about, so it now leads with the outcome set at `text-2xl` on a
surface tinted by the outcome itself — `--success` for a win, `--destructive`
for a loss, muted for a draw — with the reason under it, the rating
consequence under that, and the next action primary. It stays beside the
board rather than covering it.

Also: the clock has its own bordered container with a fixed minimum width,
so it reads as an instrument and `9:59` does not shove the name beside it;
the idle seat recedes to `bg-muted/30` so the active one is the only lit
thing; and the board took a heavier rounded frame so it is unambiguously
the object the page is about.

Verified on a real 1+0 game paired through the lobby, at 1280 light and
dark, 768 light, 360 light and dark, plus the result surface at 360 and
1280: zero page overflow everywhere, one `h1`, and nothing clipped.

**Deferred to A64-025.6B, with the reason.** Seat ratings. The data exists
and is already persisted — `game.infrastructure.models` stores
`light_rating_value` and its pair at match creation — so the gap was only
that the realtime snapshot did not carry them. Closing it is a protocol
extension plus backend tests, which is its own task rather than a corner of
a visual one. Nothing in this task guessed a rating or fetched one per
player. **Closed by §14.**

## 14. Seat ratings on the board — A64-025.6B

### 14.1 The gap, and which of the two fixes was taken

A seat named a player and showed a clock. It did not say how strong they
are, which is the fact a player wants before the first move and the one
every comparable product shows. §13.2 left two ways to close it:

| Option | Cost |
| --- | --- |
| `GET /profiles/{username}` per player | A second request each, on a different key, governed by viewer-relative privacy, on the most latency-sensitive screen in the product |
| The snapshot carries the seat ratings | A protocol extension. No new query at all |

The second was taken. `game` already stores what each seat rated when the
match was created (MT-4), the reconnect path already loads that row, and
`GameMatchSnapshot` already had the record in hand — so the values reached
the wire without a single additional read.

### 14.2 It is a match fact, not a profile read

The distinction is the whole design, and it is why this is not the privacy
shortcut it could look like:

- **It is `game`'s own column**, not a read of `rating`. No module boundary
  moved, and `rating.public.RatingReader` is not involved.
- **It is the rating at match creation**, frozen. It does not answer "what
  do they rate now", and a client must not present it as though it does.
  PR-3 already depends on that distinction for the rating calculation.
- **A rating is not privacy-governed.** UP-5: profile visibility never
  hides rated results from the opponent of those results, and
  `show_statistics` deliberately does not cover the rating itself. So
  nothing crosses that a viewer could not already see.

Spectators get it too, in the base projection, for the reason `rated` is
there: an audience that cannot tell how strong the players are is missing
what makes a game worth watching.

### 14.3 What crosses, and what stays behind

`ratings`, keyed by side like `participants`, each seat either an object or
`null`:

    ratings: { light: { value, is_provisional } | null, dark: … }

Two of the six stored fields. The Glicko-2 deviation and volatility, the
game count and the speed class are calculation inputs and a rating key — a
seat beside a board renders a number and whether it is settled. Publishing
the rest would be publishing bookkeeping, which §9 already refused for draw
cooldowns.

`value` crosses as the **stored float**. Rounding is a rendering decision,
and a server that rounded would be making it for every client that ever
reads the frame.

`null` per seat means *this match carries no rating* — every match created
before A64-017.2. The seat then shows no number and no placeholder: a dash
beside a name reads as a load that never finishes.

### 14.4 Two compatibility rules

**The field is optional on the wire.** A client that reconnects to a server
mid-rollout applies the snapshot rather than discarding a frame that is
valid in every other respect — during a deploy it is the only frame on
offer, and rejecting it would leave a live game with no board.

**A provisional rating is marked, not hidden.** `1487?`, with the word
"provisional" for a screen reader, because the mark is shorthand and
"question mark" is not what it means.

### 14.5 One test changed rather than one test weakened

`test_gateway_connection.py` asserted `"rating" not in` the snapshot
payload. That assertion encoded the requirement this task changes, so it was
replaced by a positive one plus the half that still holds: the calculation
fields and the rating key are asserted **absent**, and handles remain absent
too.

---

## 15. The board itself, and the panel around it — A64-025.6C

### 15.1 What the previous two tasks left

A64-025.6 fixed the *composition* and A64-025.6A the *surfaces beside* the
board. Neither touched the board, and a screenshot review of a real 1+0 game
found that what the page is entirely about was the one thing nobody had
designed.

Five findings, all from the running product rather than from reading the
code:

| # | Finding | Evidence |
| --- | --- | --- |
| B-1 | The board had no palette. Squares were `--foreground` at 22% opacity — the *text* colour, thinned — and pieces were hard-coded `neutral-*`. In the light theme that is a grey checkerboard; the surface read as a wireframe | `board.tsx` before this task |
| B-2 | In the dark theme a dark piece was all but invisible on a dark square, because `neutral-800` does not move when the theme does | Measured at 360 dark |
| B-3 | The quick-message group had no heading while `GameControls` beside it had one, so two labelled sections sandwiched two loose buttons | 1280 light |
| B-4 | The seat joined the rating with a space and the side with a middot — `Dark  1500?` beside `Light · You  1500?` | Both seats, every width |
| B-5 | The captured-piece ring was `ring-red-500`, a raw Tailwind colour in a product with a semantic destructive token | `board.tsx` |

### 15.2 The board is a surface with a palette, not an opacity of the text

Six tokens, defined in both themes beside every other token this product
has — `--board-light`, `--board-dark`, `--piece-light`, `--piece-light-edge`,
`--piece-dark`, `--piece-dark-edge`.

**Warm neutral, and deliberately not indigo.** The brand hue is what the
board already uses to mean *interaction*: the last move is `--primary` mixed
into the square, a legal destination is a `--primary` dot, the selected
square takes a `--primary` ring. A board tinted with the same hue would leave
every one of those competing with its own background. The board is therefore
the one surface in the product that carries a hue the brand does not.

The dark theme moves both squares down and keeps them the same distance
apart, so the checkerboard reads identically. **The dark piece is the one
value that is not a straight translation** — at the light theme's lightness
it would sit on a dark square with nothing between them, which is B-2. It
keeps a dark fill and takes a much lighter rim, so the piece is found by its
edge rather than by its body.

### 15.3 Two consequences of an opaque board

The last-move highlight was `--primary` at 18% **over transparency**, which
worked only because the square beneath it was itself a transparency. Against
an opaque board it would drop the square back to the container behind it, so
it is now mixed *into* `--board-dark` at 30% and stays opaque.

The pieces gained an inset shadow — lit from above, shaded below. That is the
whole of the relief, and it is what makes a piece read as an object on a
board rather than as a filled circle in a cell.

### 15.4 What did not change

The board's semantics, the move model, the keyboard navigation and every
accessible name. `squareLabel` still states coordinate, occupant and
interaction state, so nothing here moved information into colour. The king is
still a glyph as well as a ring — two ranks that differ by shape and not only
by tone.

### 15.5 A finding that was not a defect

The panel beside the board ends well above the board's bottom edge at `lg`,
and that was on the review list as an imbalance. It is not one: the parent
already carries `lg:items-start`, so the panel is the height of its contents
and nothing is stretching. The space beside a tall board is what a two-column
layout with unequal content produces, and the only way to fill it is to put
something in the panel that the domain does not publish — which §2 forbids
and §13.7 already refused once for a captured-material summary.

A `lg:self-start` was written and then removed. It changed nothing, and a
class that changes nothing is a claim that something was fixed.

### 15.6 Deferred, with the reason

| Gap | Why |
| --- | --- |
| Secondary player statistics in the seat | The snapshot carries ratings and nothing else about a player, by design — §14.3. Adding a win rate would be a profile read per player on the most latency-sensitive surface in the product, to decorate a card |
| Board coordinate labels | Not a regression and not asked for; the accessible name already carries the coordinate, and a rank-and-file gutter costs board size at 360 where a square is already 41px |

### 15.7 Measured

A real 1+0 game paired through the lobby in Chromium, screenshotted before
and after at every combination:

| Width | Theme | Page overflow |
| --- | --- | --- |
| 1280 | light | 0 |
| 1280 | dark | 0 |
| 768 | light | 0 |
| 768 | dark | 0 |
| 360 | light | 0 |
| 360 | dark | 0 |

`npm run test` 202 passed, `tsc --noEmit` clean, `eslint` zero errors.

Eleven selectors in `quick-messages.test.tsx` changed with the trigger's
label, which is now "Send a message" — the group heading above it says
"Quick messages", and a button repeating its own heading is a label that
tells a reader nothing. The assertions are the same assertions.

---

## 16. The bracket draws its edges — A64-025.7

### 16.1 The finding this closes

P2-1, from the A64-025.1 audit: *"the bracket has no visual parent-child
relationship, though the relationship is authoritative and already on the
wire."* §3.6 recorded the reasoning that left it out — connectors imply
absolute positioning and fixed row heights, and fixed heights are what stop a
bracket reflowing at 360px — and then said plainly that the trade was
defensible and *"the result is still not a bracket: a reader cannot see which
two nodes feed the one above them."*

### 16.2 The half of that argument that was wrong

Absolute positioning is needed. Fixed heights are not.

Each round column now distributes its own height with `flex-1` on every
node, so a round with four nodes gives each a quarter and the round beside it
with two gives each a half. A node in round N is therefore exactly as tall as
the two in round N-1 that feed it, and its centre is their midpoint **by
construction** rather than by measurement. No height is stated anywhere in the
component, and the bracket reflows exactly as it did before.

### 16.3 Every edge is the domain relationship

`bracket_plan.py` states it and the drawing consumes it unchanged:

- a node is `(round_number, slot)`;
- `BracketSlot.parent()` is `(round_number + 1, slot // 2)`;
- `takes_light_seat_of_parent()` is `slot % 2 == 0`.

That last one is the only thing the drawing branches on. An even slot is the
upper of its pair, so its line runs **down** to the midpoint the pair shares;
an odd slot's runs **up**. The two meet at the height of the node they feed,
which is where that node's incoming line already is.

Nothing measures a rendered box, and nothing infers a relationship from
position — which is what §7's principle 7 asked for: *a relationship the
domain knows is a relationship the UI draws.*

The gap between columns is `gap-8` and each stub is `w-4`. That is not a
visual preference: the two stubs and the vertical line meet in the middle of
the gap because the gap is twice the stub.

### 16.4 The lines are decoration; the text is the relationship

Every connector is `aria-hidden`. A line is invisible to a screen reader, so
the round heading and the seed number still carry the relationship in words
exactly as they did — §3.6 was right that this is the only form assistive
technology can use, and the drawing is an addition to it rather than a
replacement.

### 16.5 OQ-4, closed

**Horizontal scroll, kept.** The question was whether a phone should get a
round-at-a-time segmented view instead, and the answer is no for the reason
the original choice gave: a player comparing "who is in the semi-final"
against "who they beat" wants both columns visible, and a segmented view
hides exactly that behind a control.

What the scrolling bracket was missing was not a different navigation model.
It was the edges, and they are drawn now at every width.

### 16.6 Measured

The bracket of a real eight-entrant tournament, seeded and started through
`python -m app.operator.tournament run`, so every node, seed and live match
is a server fact:

| Width | Theme | Page overflow |
| --- | --- | --- |
| 1280 | light | 0 |
| 1280 | dark | 0 |
| 360 | light | 0 |
| 360 | dark | 0 |

The page body still never scrolls sideways; the labelled, focusable scroller
is still the only thing that does.

`npm run test` 202 passed, `tsc --noEmit` clean, `eslint` zero errors.

### 16.7 Two defects the same review found on the surfaces around it

**The variant and the speed class were raw server enums.** A tournament card
said `russian_8x8` and `classical` to a player, in every locale, on the lobby
and on the detail page. Nothing failed — the identifiers simply arrived on
screen, which is exactly the failure `labels.ts` was written to prevent and
which its own note describes.

The keys live under `play.*` rather than `tournament.*` because that is where
this vocabulary already was: `play.speed` has carried all five classes since
the lobby was built, and a second copy would be two places to add
`correspondence`. `play.variant` is new only because no surface had ever
needed to name a variant — the game room does not, since a player who is
playing knows what they are playing.

**The status was drawn two ways.** The lobby card had a bordered pill; the
detail page put the same fact into a run-on subtitle, so the surface a player
lands on after clicking a card dropped the treatment the card had just taught
them. One `TournamentStatusBadge` now serves both, and `in_progress` gains
`--success` — it had no tint at all, though it is the state a spectator is
looking for.

### 16.8 The entrants list, and why there is not one

**Who has entered an open tournament is not visible anywhere.** The detail
page says "Entrants: 3 of 8" and the bracket carries `participants` — but a
bracket exists only after registration closes and the field is materialised,
so for the whole period when the question actually matters, the answer is a
count.

It stays that way here, because there is no endpoint. `GET /tournaments/{id}`
publishes `entrant_count` and nothing else about who they are; the read
surface is `/bracket`, `/standings` and `/registrations/me`. Serving the list
means a new contract, and a backend contract change is this task's stated
non-goal — as it should be: an entrants list is a paginated read over a
table with its own privacy question (whether entering a tournament is public
is a product decision nobody has made), not a corner of a visual task.

Recorded rather than quietly skipped: it is the one thing a player looking at
an open tournament cannot find out.

### 16.9 Two smaller corrections on the same page

**"Round: —" on a tournament that has not started.** The row rendered an em
dash where `started_at` and `completed_at` beside it are omitted entirely
when absent. A dash in a definition list reads as a value that failed to
load, so the row now follows its neighbours' convention.

**The standings table was left alone deliberately.** It is well built —
`sr-only` tie announcements, the viewer's row highlighted, tabular figures,
a horizontal scroller for its eight columns — and it renders only for a
completed tournament, which this task had no way to put on screen. Changing
a surface nobody could look at would have been the one kind of work this
epic has avoided throughout: a claim that something was improved, unbacked
by having seen it.

---

## 17. Friends and social — A64-025.8

Reviewed the way §15 and §16 were: the five surfaces driven in a browser
against seeded relationships — two friendships, an incoming request, an
outgoing one and a blocked player — rather than read as code.

### 17.1 Blocking was drawn like declining

`Block` sat beside `Decline` and beside `Cancel request` at identical
weight. They are not the same kind of act: one answers a question, the other
ends a relationship and takes a confirmation dialog to undo. A player
skimming a request row had nothing to tell them apart.

The tone is the one the game room already gives Resign — destructive
**text**, not a red slab a thumb finds by accident in a dense list — and the
branch is `isDestructive`, the predicate this file already imports to decide
which actions open a dialog. Removing a friend gets it for the same reason.

### 17.2 A name over its own handle

`PlayerRow` rendered the display name and then `@username` beneath it. Most
accounts have no display name, so `nameOf` falls back to the username and
the row showed `alice` over `@alice` — two lines carrying one fact, on every
social surface, since the row was written.

The handle now renders only when it says something the line above did not.
The rule is stated as a test rather than left to a reviewer's eye: a friend
with a display name shows both, one without shows the name once.

### 17.3 A question for the backend, not a fix here

The blocked list shows a blocked player's **presence**. `is_online` arrives
on that row and `PlayerRow`'s rule is to render whatever the API sent and
default nothing — so the client is behaving correctly, and changing it here
would be this module deciding a privacy rule that belongs to `friends`.

Whether somebody you blocked should still report as online to you is a
product decision. It is recorded here rather than patched in the view.

### 17.4 Measured

Five surfaces — friends, requests, blocked, search, challenges — at 1280
light and 360 dark, against seeded relationships:

| Surface | Page overflow |
| --- | --- |
| `/friends` | 0 |
| `/friends/requests` | 0 |
| `/friends/blocked` | 0 |
| `/search` | 0 |
| `/challenges` | 0 |

`npm run test` 203 passed — one more than before, and it is the handle rule.
`tsc --noEmit` clean, eslint zero errors.

### 17.5 An empty state that names an action now offers it

"Find players and send them a request" named something to do and left the
player to find the way. The control was in the navigation beside it and
nowhere the sentence pointed.

`ListState` gains an optional `emptyAction`, and **the option matters more
than the slot**: only two of this product's empty states pass one. The
friends list offers Search, and the sent-challenges tab offers the friends
list, because both hints name something the player can do. The incoming
tab's does not — "when a friend invites you to a game, it appears here"
describes waiting, and a button under it would be an invented next step. The
blocked list and the two request lists are the same: nothing to offer, so
nothing offered.

The rule is a test, not a convention a reviewer has to hold: the friends
empty state must expose a link to `/search`.

### 17.6 What was seen, and what was not

**Search was reviewed and needed nothing of its own.** Twenty results
against a real term at 1280: the rows carry both fixes above — one name line
where there is no display name, `Block` in the destructive tone — and the
page does not overflow. What it does raise is a *product* question rather
than a visual one: a stranger's row offers `Block` beside `Add friend` as a
co-equal action, which is `actionsFor` returning what the relationship
allows rather than what a search result should lead with. Left alone,
because narrowing it is a decision about the social model.

**The challenge rows were never seen.** Two pending challenges were seeded
directly into `matchmaking.friend_challenge` — one sent, one received — and
neither reached the list. The read filters them for a reason this task did
not chase: seeding a friend challenge through the database alone evidently
misses something the service does, and the honest record is that the rows
are unverified rather than that they are fine.

Both empty states *were* verified, and they are the two this task changed.

The challenge rows themselves are the remainder of `.8`.

---

## 18. Profile — A64-025.9

### 18.1 The one page in this epic that overflowed

Every surface `.6C` through `.8` measured zero horizontal overflow. `/profile`
at 360 measured **47px**, and the cause was the avatar upload's raw
`<input type="file">`: its intrinsic width does not shrink, so it pushed the
page past the viewport on the narrowest screen the product supports.

The same control carried a second defect that no measurement would have
caught. The words inside it — "Choose File", "No file chosen" — are the
**browser's**, not the product's. They are English in every locale, on a page
where all 775 other strings go through `t()`.

### 18.2 The input stayed an input

The component's own note argued for a real `<input type="file">` and against
"a hidden input behind a `<div onClick>`": the native control is keyboard
reachable, announces its accepted types, and opens the system picker on
Enter, and a div does none of that. That argument is correct and nothing
here contradicts it.

The input is therefore **visually** hidden rather than replaced — `sr-only`
leaves it in the tab order and in the accessibility tree — and the `<label>`
already bound to it is what a pointer hits and what carries the text this
product owns. `peer-focus-visible` on the label is what keeps a keyboard
user's focus visible now that the control itself is not drawn.

The avatar test passes unchanged, which is the check that matters: it finds
the input through its label, exactly as assistive technology does.

### 18.3 A rule applied where it had been missed

`PlayerRow` renders `@username` only when the display name differs from it —
A64-025.8 §17.2. `ProfileHeader` did not, so a profile printed `alice` over
`@alice` for every account without a display name, which is most of them.
The same condition now guards both.

### 18.4 Measured — the defect pass

| Surface | Width | Theme | Page overflow |
| --- | --- | --- | --- |
| `/profile` | 1280 | light | 0 |
| `/profile` | 360 | dark | **0** — was 47 |
| `/players/{username}` | 1280 | light | 0 |
| `/players/{username}` | 360 | dark | 0 |

`npm run test` 204 passed, `tsc --noEmit` clean, eslint zero errors.

### 18.5 The open question, answered — the full visual rework

§18.4 above closed a *defect* pass. What follows is a **redesign**, asked for
explicitly and carried out on this surface first so that it can become the
template the remaining surfaces are brought up to.

The open question was the ratings block: five cards, each `1,500`, each
captioned "not rated yet", occupying a third of the page in the largest type
on it. The earlier answer was that the data was correct and the epic's rule
was to change only what is demonstrably wrong. **That rule is retired here.**
A surface whose every number is accurate and which still fails to tell a
reader who this player is has a real defect; it is simply not one a
measurement finds.

#### What was wrong, and what each part now does

| Was | Is |
| --- | --- |
| The page opened with "Joined 5 August 2026" and a file upload | It opens with the name and the leading standing, set as the two largest things on it |
| The avatar was drawn **twice** — as identity, and again inside the upload control | Drawn once. `AvatarManager` moved to `/settings/profile` |
| Seven statistics at one weight, all `text-sm` | Three headline figures; wins/losses/draws as one proportional bar with a legend; highest rating as a footnote |
| Five identical rating cards, `1,500`, "not rated yet" | Cards for categories with games; **one line** naming the rest |
| The speed class printed the raw server enum — `blitz`, in every locale | `speedClassKey`, translated |
| `highest_rating` showed `1,500` for an account that had played nothing | Rendered only once something has been played |

#### The three decisions worth recording

**The headline standing is the most-played category, not the highest
number.** `/ratings/me` returns every speed class, seeding the unplayed ones
at 1,500 — so "highest" would crown a category nobody has entered, and
"first" would crown whatever order the API sent. `primaryRating` in
`entities/profile` picks the one with the most games, ties broken by rating,
and its doc comment says why. It is presentation only; every rating stays
exactly what the server sent.

**Played and unplayed are different kinds of thing, so they are drawn
differently.** A category with games is a measurement and gets a card. A
category without them is an invitation, and the honest form of that is one
line — not five cards impersonating results. A brand-new account now reads
as a short, calm page rather than a wall of numbers nobody earned.

**`/profile` shows; `/settings/profile` edits.** The avatar was the one
editable thing not reached through "Edit profile", which is what produced
the duplicate. Moving it removes nothing: the control, its label, its size
check and its test are unchanged — only the route that renders it.

`speedClassKey` moved from `features/tournament/ui/labels.ts` to
`entities/time-control`, where the `SpeedClass` type already lives. Three
surfaces now read it; a second copy beside the ratings would have been the
divergence §3.4 forbids. `formatList` was added to `shared/lib/format.ts`
because joining the unrated categories with `", "` is a Latin-script
assumption `Intl.ListFormat` already knows better than.

### 18.6 Measured, after the rework

Overflow at three widths, in **both** the played and the brand-new-account
states — the second is the one the old ratings block was worst on:

| Surface | 1280 light | 768 light | 360 dark |
| --- | --- | --- | --- |
| `/profile`, account with games | 0 | 0 | 0 |
| `/profile`, new account | 0 | 0 | 0 |
| `/players/{username}`, account with games | 0 | 0 | 0 |
| `/players/{username}`, new account | 0 | 0 | 0 |

`npm run test` 204 passed, `tsc --noEmit` clean, eslint zero errors — and
re-run unchanged after §18.7's palette change. The profile suite gained an
assertion that the band leads with the played
category and never with an unplayed 1,500; the avatar test moved to
`/settings/profile`, unchanged otherwise.

### 18.7 Colour — the brand gradient, and where it is allowed

The rework above left the surface correct and almost entirely neutral. The
brand colour existed, was decided in A64-025.2 (OQ-2), and was used in 29
places across tournaments, the game room and history — and in **none** on a
profile. Colour was therefore not missing from the product; it was missing
from the surfaces a player spends time on.

#### The decision

`--primary` is unchanged and still carries every functional accent: the
focus ring, a live match, a selected option, a legal destination on the
board. Added beside it is the **display** form of the same brand — a
gradient from the existing indigo to a magenta, plus `--rating`, an amber
that means *a personal high* and is kept apart from `--warning` because a
best streak is not a caution.

`--background` also picked up a trace of the brand hue, in both themes. It
is far below the threshold at which anyone would call the page coloured; its
job is to let a white `--card` read as a raised surface instead of
dissolving into the page, which the borders were doing alone. These are the
only two departures from the shadcn neutral base the file header documents,
and the header now says so.

#### Gradient is rationed, and the rule is written down

Three places, all of them brand:

| Where | What |
| --- | --- |
| The wordmark | `brand-gradient-text`, in the header and on the auth front door — one wordmark, one treatment |
| The primary action | `Button` `variant="default"` |
| The auth panel | replaced a flat `bg-primary` on the same element, same foreground |

Everywhere else is solid, and that is a cost decision rather than a taste
one: **contrast over a gradient cannot be asserted in a test, only looked
at — and then looked at again in the other theme.** So both ends of the ramp
are pinned to values that clear 4.5:1 against `--primary-foreground` on
their own (the magenta end is darker than a magenta wants to be for exactly
that reason), and the rule is that no text sits over a gradient whose two
ends it has not already cleared. The gradient is defined once, in
`globals.css`, so its angle and stops cannot drift between the four.

`brand-gradient-text` degrades to the solid brand colour where
`background-clip: text` is unsupported — never to invisible text.

#### One thing that did not work

`--rating` was first applied to the best-streak **figure**. An amber dark
enough to clear 4.5:1 on white is brown by the time it gets there, and a
brown numeral reads as a rendering fault rather than as a highlight. The
tint moved behind the figure — the cell is washed and the label carries the
hue, so the number keeps the card's own contrast. Recorded because the same
trap is waiting for every other "let us make this one gold".

#### The second vocabulary: a hue per speed class

The brand gradient says *Arena64*. It does not help anyone read a page, and
on a profile the ratings block stayed almost entirely neutral under it. So a
second set of tokens carries **category**: one hue per speed class, hot at
the fast end and cool at the slow end, applied wherever the product names a
class.

| Surface | Before | After |
| --- | --- | --- |
| A profile's rating cards | neutral label | a 4px rule and a label in the class's hue |
| A profile's leading figure | brand wash | the hue of the class it is a rating in |
| Match history | the **raw enum** — `blitz`, in every locale | the translated name, in the class's hue |
| Tournament card and page | neutral text | the same |

That is the point of it: the colour means the same thing on all four, so a
returning player finds Blitz by colour before reading the word. Values are
tuned for text on a card — L≈0.48 light for 4.5:1 on white, L≈0.74 dark for
the same on `--card` — and one token serves both the label and the rule, so
the two cannot drift apart.

**The leading figure takes the class's hue rather than the brand's.** It is
a rating in that class and the card for it sits further down the same page;
a brand-purple panel above an orange Blitz card would be one fact wearing
two colours. That wash is the only gradient outside the brand's own four
places, and it is 15% — nothing is measured against it.

#### Three vocabularies, and the rule that keeps them apart

| Token family | Means | Where |
| --- | --- | --- |
| `--primary` | *interaction* | focus ring, live match, selected option, legal destination |
| `--brand-from`/`--brand-to` | *Arena64* | wordmark, primary action, auth panel, and nothing else |
| `--speed-*` | *category* | anywhere a speed class is named |
| `--rating` | *a personal high* | best streak, highest rating |

No surface uses more than two of them at once. That constraint is what stops
this becoming the wall of colour the ratings block used to be a wall of
numbers.

### 18.8 Closing the surface

Four things the rework left standing, and what each now is.

**The tournament history was three facts at one weight.** A name, a date and
a rank, spread by `justify-between` so the date floated in the middle of the
row with nothing to align to. The response already carried the speed class,
the format and `final_status`, and none of it was rendered — so "Rank 1" and
"Champion" were the same information published once, in the weaker of the
two forms. Now the placing leads as a fixed-width chip (fixed, because a
chip that sizes to its own text starts every name at a different x), the
name is the row, the speed class and format and date are one subordinate
line, and the outcome is stated in words. Gold marks a win with the word
"Champion" beside it. Below `sm` the three stack instead of competing; the
old layout truncated a tournament to "Autumn Blitz C…" at 360.

**Destructive actions had the same weight as the thing a visitor came for.**
On a public profile, "Add friend" and "Block" were both outlined buttons of
equal size. `RelationshipActions` now has three weights rather than two: the
affirmative act (`send_request`, `accept_request`) leads as the primary
button, and `isDestructive` actions drop to `ghost` — destructive text, no
border, the quietest thing in the group. That predicate already decided
which actions open a confirmation dialog, so nothing new decides anything.

**Three actions wrapped raggedly at 360.** `flex-wrap` put "Match history"
alone on a second line, indented by a ghost button's own padding, which
reads as an accident rather than as a third action. A two-column grid below
`sm` makes the wrap deliberate: two, then one across.

**The photo control was three loose siblings.** The avatar, the upload
label and the removal button were all children of the section, so "Remove
photo" sat under the hint text with nothing tying it to the picture it
removes. They are one carded block now, with removal reduced to the same
ghost weight §18.8 gives blocking. The input itself is untouched — still a
real `<input type="file">`, still `sr-only` with its label as the target,
and its test still finds it by label exactly as assistive technology does.

One consistency fix went with them: hidden statistics, an empty tournament
history and the unrated categories now share a single dashed frame. Each was
a bare paragraph between two carded sections, which reads as text that lost
its container rather than as a stated absence.

### 18.9 Measured, closed

Both profile surfaces, three widths, and the two states the ratings block
behaves differently in:

| Surface | 1280 light | 768 light | 360 dark |
| --- | --- | --- | --- |
| `/profile`, account with games | 0 | 0 | 0 |
| `/profile`, new account | 0 | 0 | 0 |
| `/players/{username}`, account with games | 0 | 0 | 0 |
| `/players/{username}`, new account | 0 | 0 | 0 |

`npm run test` 204 passed, `tsc --noEmit` clean, eslint zero errors.

**A64-025.9 is closed for `/profile`, `/players/{username}` and
`/settings/profile`.** The remaining four settings surfaces
(`/settings/{preferences,privacy,notifications,sessions}`) are not
redesigned; they are the next surfaces to take this template — see `L-1` in
`specs/README.md`, which records that they also ship against no spec.

---

## 19. Home, and the account menu — A64-025.9B

Not in §5's original plan. The phase exists because closing `/profile`
exposed the two surfaces around it: the header every page carries, and the
first screen a player lands on.

### 19.1 The header was five controls for one idea

Counted on a signed-in page at 1280: the avatar, the name, a sign-out
button, and a three-button theme group. Five targets, four of them account
or browser settings, and the theme group alone was three.

Worse than the count was the ranking. **Sign-out sat beside the player's own
name**, as though leaving were a peer of arriving — the single most
destructive thing in the shell, given the same weight as the link to your
profile. And the **language** could be changed only from
`/settings/preferences`, four clicks deep, in a product that ships in three.

| Was | Is |
| --- | --- |
| avatar · name · Sign out · ☀ · ☾ · 💻 | avatar · name · ⌄ |
| Theme: three header buttons | inside the menu, still three explicit choices |
| Language: `/settings/preferences` only | inside the menu |
| Sign out: beside the name | inside the menu, ghost, destructive text, last |
| The avatar: **initials for everyone** | the player's photo |

### 19.2 Three decisions

**A dialog, not a new dependency.** The same call `MobileNav` recorded: Radix
Dialog is already here and already wrapped, and it brings the four things a
menu must not get wrong — focus trap, focus returned to the trigger,
`Escape`, and `aria-expanded`/`aria-controls` wired between trigger and
content. Positioning it under the trigger instead of centred is a
`className`, not a second primitive. `@radix-ui/react-dropdown-menu` was the
alternative and would have been a dependency for what one already installed
does.

**Theme and language stay reachable when signed out — and when the session
is `unavailable`.** Neither is an account preference: both are properties of
the browser in front of the player, stored in `localStorage`. A player
staring at a page the server could not fill is exactly the one who might
want the interface in another language, so the appearance menu renders in
every state that is not `authenticated`. The sign-in link still renders only
for `anonymous`, which is the one state that actually means "there is no
session".

**The photo needed a request the shell was not making.** `SessionUser`
carries no `avatar_url` — the bootstrap response has never included one — so
the header drew initials for every player, including those who had uploaded
a picture. The menu now reads `/profile/me`, the query `/profile` already
fills, mounted only when authenticated so an anonymous visitor cannot fire a
request that could only 401.

That second consumer surfaced a test that was asserting the test
harness rather than the product: `createTestQueryClient` sets
`staleTime: 0`, so two consumers mounting a tick apart fetch twice where
production's 30-second window fetches once. The assertion now states the
invariant it was always about — *the save added no read* — rather than an
absolute count.

### 19.3 Home had no reason to be visited twice

A heading, a sentence, one button and four cards. Every word of it was true
and none of it was about the player.

§3 forbade any query here, and its reasoning was sound: a dashboard would
have to invent its figures — an online-player count, a recommended
tournament — and a plausible number the server never sent is worse than an
empty page. **Nothing invented has been added.** What has been added is the
player's own standing: `/profile/me` and `/ratings/me`, the same two
requests `/profile` makes, already cached on any second visit, the first of
them already fetched by the header. A home page that cannot tell a returning
player how strong they are is not being disciplined; it is being empty.

The strip renders only when both have landed and never renders a skeleton —
a placeholder that pushes the primary button down the page for 200ms is
worse than a strip that appears.

### 19.4 The product now draws its own board

There was no artwork anywhere in the repository. `BoardMotif` is an inline
SVG of a board fragment with three men on it, drawn in `--board-light`,
`--board-dark` and the two piece tokens, so it follows the theme and any
future change to the board's palette without a second asset to keep in step.
A PNG would be two files that drift.

It is `aria-hidden` and carries no information; everything the section says
is said in text beside it.

The first attempt drew it at 18% opacity and it read as a rendering fault.
It is at full strength now — the board's own tokens are a muted warm beige
to begin with, so it sits behind the text without ghosting, and a product
that shows its own board apologetically is not showing it.

The destination cards took icons and a whole-card click target, done with
`after:inset-0` on the one link rather than an anchor wrapped round the
heading: the card is the target and there is still exactly **one** link,
with the section's name as its accessible name.

### 19.5 Measured

| Surface | 1280 light | 360 dark |
| --- | --- | --- |
| `/` signed in | 0 | 0 |
| `/` signed out | 0 | 0 |
| `/` with the account menu open | 0 | 0 |

204 tests pass, `tsc --noEmit` clean, eslint zero errors. Four suites
reached sign-out through the header and now open the menu first through one
shared `openAccountMenu` helper — the behaviour each asserts is unchanged;
only the path to the control is.
