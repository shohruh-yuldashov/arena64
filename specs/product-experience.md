# Feature Specification — Product Experience

| Field | Value |
| --- | --- |
| **Spec ID** | `SPEC-PRODUCT-EXPERIENCE` |
| **Status** | Draft — .1 audit; .2 foundation; .3 shell; .4 auth; .5 lobby; .6…​.6C game room; .7 tournament; .8 social; .9 profile |
| **Owner** | _Unassigned_ |
| **Created** | 2026-08-10 |
| **Last updated** | 2026-09-05 — A64-026.2, brand identity |
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
**Fixed by A64-025.6** — `PlayerSeat` carries `LOW_TIME_SECONDS`, a `--warning` clock and
the word beside the colour. Verified in A64-025.13; this line had read "future task"
for four phases after the work shipped (§35.2).

**P1-3 — On a phone the clocks sit below the board.**
The game page stacks board-then-panel until `lg:`. On the surface where time matters
most, the clock can be off-screen.
**Fixed by A64-025.6** — the seats sit directly above and below the board at every
width. Verified in A64-025.13 (§35.2).

### P2 — a noticeable UX problem

| # | Finding | Task |
| --- | --- | --- |
| ~~P2-1~~ | ~~Bracket has no visual parent-child relationship, though the relationship is authoritative and already on the wire (§3.6)~~ | **Fixed** — §16 |
| ~~P2-2~~ | ~~Password-reset email is English-only and plain-text-only~~ | **Closed by A64-025.10E** — trilingual, both parts, on the shared shell (§30) |
| ~~P2-3~~ | ~~No shared empty/error/loading component — 74 hand-rolled live regions (§3.9)~~ | **Closed by A64-025.11** — `LoadFailure` added, `ListState` made to fit, six surfaces swept (§32). The live regions that remain are not load states |
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
| ~~P3-5~~ | ~~No `prefers-reduced-motion` handling — correct today, wrong once motion is added~~ | **Fixed** — one motion scale, two sources, the more conservative wins (§34) |
| P3-6 | Eight primitives; Badge, Select, Tabs, Tooltip, Dropdown, Switch re-authored per feature | **Moot** — measured in A64-025.13 (§35.2): `shared/ui` holds 12, none of those six Radix parts is installed or used, and what remains is three domain chips |

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
| **A64-025.5B** | The lobby, and the board preferences that did nothing | .2, .5 | Changing what a preference means; the three still unread |
| **A64-025.5C** | Match history and replay | .2, .5B | Changing what a replay reconstructs |
| **A64-025.5D** | The same surfaces, read in Uzbek and Russian | .5C, .10 | Retranslating; changing layout English needs |
| **A64-025.7B** | The tournament list, and the browser's missing Uzbek | .7, .5D | Number formatting; hand-writing what ICU gets right |
| **A64-025.7C** | The tournament page the list links to | .7B | The bracket, which §16 settled |
| **A64-025.8B** | The five social surfaces, after the card system | .8, .9 | Relationship rules, which §17 settled |
| **A64-025.6D** | The game room, read against a live server | .6C, .5B | The board's layout, which §15 settled |
| **A64-025.4B** | The front door — what a signed-out visitor is offered | .4, .3 | The auth forms, which §11 settled |
| **A64-025.9B** | Home, and the account menu in the header | .2, .3 | Inventing a statistic the API does not return |
| **A64-025.9C** | The four remaining settings surfaces | .2, .9 | Changing what any setting does |
| **A64-025.10** | Notifications — the feed and the bell | .2 | Admin notification surfaces; what the preferences decide (.9C) |
| **A64-025.10E** | Email design system. Fixes P2-2 | .2 (tokens only) | New email types |
| **A64-025.10F** | The email shell, designed rather than merely shared | .10E | New email types; a dark variant |
| **A64-025.11** | Global UI consistency and component cleanup. Fixes P2-3 | .3–.10 | Re-architecting layouts already designed mobile-first |
| **A64-025.12A** | A throw inside the router reaches this app's error page | — | The unreproduced i18n context fault itself (§33.3) |
| **A64-025.13A** | The match a no-show left open, and the port that closes it | .13 | A moderator's adjudication of a *played* game, which needs an audit trail |
| **A64-025.13B** | Every context object in a module Fast Refresh will not swap | .12A | `features/auth`'s session context (§37.3) |
| **A64-025.14** | `confirm_move`, the fifth gameplay preference | .6D | Editing a staged move (§38.5) |
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

---

## 20. Settings — A64-025.9C

The four surfaces `L-1` in `specs/README.md` records as shipping against no
spec: `/settings/{preferences,privacy,notifications,sessions}`. Behaviour is
unchanged on all four. What changed is that they now look like the product
they are part of.

### 20.1 One shape, four pages

Each was a flat column of labels and controls. At 1280 a 500px select sat
inside a 1160px column, so two thirds of every row was empty and the eye had
no right-hand edge to follow; nothing grouped the settings except vertical
spacing, and spacing alone does not say *these three belong together*.

`SettingCard`, `SettingRow` and `SettingGroup` in `shared/ui` are the answer,
and it is the same card the profile's statistics and ratings already use, so
settings stop looking like a different product. A row puts the name and its
consequence on the left and the control on the right; below `sm` it stacks,
**except** for checkboxes, where `inline` keeps the row horizontal — a tick
is small enough to sit beside its label at 360, and stacking one leaves it
floating under a sentence with nothing to attach it to.

`descriptionId` is a prop rather than something the row invents, because the
caller owns the control. Privacy's checkboxes need the consequence in their
accessible description — a label reading "Show my country" without saying
*where* leaves somebody agreeing to they-know-not-what — and a row whose
description merely repeats its label needs no reference at all. A dangling
`aria-describedby` is worse than none: it resolves silently to nothing.

### 20.2 Ten raw enum values, in three languages

`Preferences` printed the server's own identifiers as option labels:
`classic`, `wood`, `marble`, `midnight`, `modern`, `neo`, `instant`, `fast`,
`normal`, `slow` — and `UZ`, `RU`, `EN` for the language itself. A
`capitalize` class was papering over it, which is not a translation: it
produced "Classic" in Russian too.

Eleven keys per locale now, and the language names come from `localeName`,
which already existed for exactly this and had one caller. This is the same
defect A64-025.7 fixed for the speed class and A64-025.9 fixed for the
ratings cards; it is the third time, which is why it is written down as a
class of defect rather than three incidents.

### 20.3 Nine copies of three sentences

`/settings/notifications` printed every channel's description inside every
category: three categories × three channels, plus the email caveat twice
more. The meaning of a channel does not change per category — that is what a
channel *is* — so it is stated once, in a key above the grid, in the reading
order somebody meets before their first checkbox.

A cell is left with the one thing that genuinely is per-cell: why *this*
switch is locked. The channel list in the key comes from the settings the
server sent rather than a hardcoded array, which is the rule
`groupByCategory` already followed for categories.

**The fieldsets stayed.** §21's argument for them over a `<table>` is
correct and nothing here contradicts it: a table header and its checkbox end
up on different screens at 360, and a legend is announced before every
control it contains, so "Email" is never heard without knowing email *of
what*. The repetition was never the grouping's fault.

The suite's assertion moved with the sentence and got stricter: it asserted
the caveat appeared inside the social group and now asserts it appears
**exactly once** on the page. The behaviour it protects — nobody turns email
on without being told what a verified address means — is unchanged.

### 20.4 The one control on `/settings/sessions` was the loudest in the product

A full-weight red slab, alone on an otherwise empty page, so the page read
as a warning rather than as a setting. It is a ghost button with destructive
text now — the weight §18.8 gives blocking a player and removing a photo —
and the confirmation dialog behind it is what actually guards the act, as it
always was. The "no device list yet" note took the dashed frame every other
stated absence in this product uses.

Two more things joined the system while they were open: the notification
form's save and discard buttons were the only primary and ghost controls in
the product spelled out in utility classes, so they had kept the flat brand
colour when A64-025.9B gave `variant="default"` the gradient — they are
`Button` now. And the app-install section, which was a rule and two
paragraphs, is a group and a card.

### 20.5 Measured

| Surface | 1280 light | 360 dark |
| --- | --- | --- |
| `/settings/preferences` | 0 | 0 |
| `/settings/privacy` | 0 | 0 |
| `/settings/notifications` | 0 | 0 |
| `/settings/sessions` | 0 | — |

204 tests pass, `tsc --noEmit` clean, eslint zero errors.

**A64-025.9 is now closed on every settings surface.** `L-1` remains open:
these four still ship against no specification, and this section describes
how they look, not what they do.

---

## 21. The notification feed — A64-025.10

`/notifications` and the bell. The preferences that decide what arrives were
A64-025.9C's; this is the list of what did.

### 21.1 An absolute timestamp answers the wrong question

Every row read `Sep 4, 2026, 1:40 PM`. A feed is read for **recency**, and
that string is the same number of words as "2 hours ago" while making the
reader do the subtraction. The relative form is now the text and the exact
instant stays on the `<time>` element's `dateTime` and `title` — demoted,
not removed.

`Intl.RelativeTimeFormat` supplies every word, including "yesterday" and
"last week", so this added **no translations to maintain** and is correct in
locales whose plural rules are not English's.

> **Corrected by A64-025.5D §24.2.** That claim was true of English and
> Russian and false of Uzbek, which is this product's first language.
> Chromium reports `uz` as supported and then has no patterns for it. The
> sentences are the product's own now; `Intl.PluralRules` still picks the
> form.

**The first version was wrong and the test is the proof.** It divided
elapsed seconds by 86,400, so 46 hours produced "yesterday" while the day
heading above the same row produced "2 days ago" — both true of different
quantities, and visibly contradictory. Days are calendar days now, measured
between local midnights, and only hours and below come from elapsed time.
`shared/lib/format.test.ts` pins that invariant, along with the "now"
boundary and the climb to weeks, months and years; the dates are built from
local parts rather than `Z` literals, because a UTC literal makes a
calendar-day assertion depend on the machine's zone.

### 21.2 Grouped by day, in one list

| | |
| --- | --- |
| **Today / Yesterday** | `Intl`, `numeric: "auto"`, capitalised for the locale |
| **Inside the last week** | the weekday — "Wednesday" beats "4 days ago", which is arithmetic |
| **Older** | the date, because the words have stopped helping |

**One list, not one per day.** A section per day was written first and cost
five test failures, which was the right signal: every row already carries
its own `<time>`, so a screen reader loses nothing by not hearing the
separator, and a single list keeps a length that means *the number of
notifications* rather than however many days they happen to span.
`role="presentation"` is what takes the separator out of the list without
taking it off the screen.

### 21.3 Unread was a dot 900 pixels from its sentence

The unread marker sat against the right margin of a wide row with nothing
connecting it to the message it belonged to. It is a tinted band across the
whole row now, and the dot and the `sr-only` word both stay — three signals
where colour was never the only one.

"Mark all as read" was alone against the right margin too. It sits beside
the count it answers ("3 unread") now: a control with nothing to align to
reads as an afterthought.

The rows were floating on the page background with hairline rules and
nothing containing them — the last list in the product that was not in a
card. The empty state took the dashed frame every other stated absence uses.

### 21.4 One thing tried and removed

A subtitle under the heading, borrowing `emptyDescription` — "New
notifications will appear here." It is a sentence written for an empty list
and reads as nonsense above a full one. A heading with nothing useful under
it is better than a heading with the wrong thing.

### 21.5 Measured

| Surface | 1280 light | 360 dark |
| --- | --- | --- |
| `/notifications`, five entries across three days | 0 | 0 |
| `/notifications`, empty | 0 | — |

213 tests pass (nine of them new, for the two formatters), `tsc --noEmit`
clean, eslint zero errors.

---

## 22. The lobby, and the settings that did nothing — A64-025.5B

### 22.1 Five preferences were write-only

`PreferencesResponse.gameplay` has carried `board_theme`, `piece_set`,
`animation_speed`, `confirm_move` and `show_coordinates` since A64-012.5.
A search of the client finds **exactly one reader for each: the form that
writes it.** Nothing else in the product has ever looked at any of them.

A player chose "Wood", the form said "Saved", the server stored it, and
every board stayed exactly as it was. That is worse than a missing setting:
a missing setting is honest.

Two of the five are closed here. **Three are not**, and are recorded rather
than quietly left:

| Preference | State |
| --- | --- |
| `board_theme` | **Closed** — four palettes, applied to every board |
| `piece_set` | **Closed** — three finishes, applied to every piece |
| `show_coordinates` | **Closed** by A64-025.6D §28 |
| `animation_speed` | Still unread. There is no move animation to speed up yet — A64-025.12's |
| ~~`confirm_move`~~ | **Closed by A64-025.14** — §38. It does change move *submission* rather than presentation, which is why it needed a step rather than an attribute |

### 22.2 Tokens on the root, not props through four components

A board is drawn in the game room, in the lobby's preview, and wherever one
is drawn next. Threading two strings to each is three places to forget, so
`BoardPreferences` writes `data-board-theme` and `data-piece-set` on
`<html>` and the palettes live in `globals.css` beside the tokens they
override. It follows `shared/theme`, which puts the `dark` class on the same
element for the same reason, and it is mounted behind the session check
because the preference is an account read that can only 401 without one.

`classic` has **no block**: it is the base in `:root`/`.dark`, and a
duplicate set of values for it would be the first pair to drift.

**A piece set changes the finish, not only the colour.** The radius, the rim
width and the relief were literals in `board.tsx`, which meant `piece_set`
could only ever have been three names for one disc. They are tokens now —
`--piece-radius`, `--piece-border-width`, `--piece-shadow` — so "modern" is
genuinely flat and "neo" genuinely squared. What a set does not change is
the silhouette a *king* sits on: the king is told apart by a glyph, and a
set that changed the outline would land that glyph on three different
shapes.

`PIECE_FINISH_CLASS` lives in `entities/board`, not in either component that
draws a piece. The first attempt put it in the preview widget and had
`features/game` import from `widgets/` — backwards, and caught by nothing
but reading it.

### 22.3 The preview had to be a board, not a drawing

`BoardMotif` — the home page's artwork — was the obvious thing to reuse and
would have been wrong. Its pieces are SVG `<circle>`s, so a radius, a rim
and a relief do not survive them: the preview would have shown the colours
of "neo" on the shape of "classic", which is a preview that lies about the
thing it previews.

`BoardSample` is a four-by-four grid of the same elements with the same
classes the real board uses, reading the same tokens. That is also what
stops the two drifting: a piece set added later appears in the preview
without anybody remembering there is a second drawing.

It sits **below** the form and above the friend link. It is information, not
an action, and it must not come between a player and the button they opened
the page for.

### 22.4 The lobby's time controls carry their speed class

`Bullet`, `Blitz`, `Rapid` and `Classical` were four grey words under four
clocks. They are in their own hues now — the same ones the profile's rating
cards, match history and the tournament surfaces use, so a player recognises
Blitz here before reading it. A chosen tile drops back to `--primary`,
because the selection is already saying something in that colour and two
colours on one tile is neither.

### 22.5 One link removed

`/profile` offered "Match history" beside "Edit profile". Match history is a
**section** of the product: it is in the header at every width, and offering
it a second time on one profile out of many is a navigation model
disagreeing with itself. Removing it also let the action row divide evenly,
which the grid had been working around with a `col-span-2` on the odd one
out.

### 22.6 Measured

| Surface | 1280 light | 360 dark |
| --- | --- | --- |
| `/play` | 0 | 0 |

Four board-and-piece combinations rendered and compared: classic/classic,
wood/neo, marble/modern, midnight/neo. 213 tests pass, `tsc --noEmit` clean,
eslint zero errors.

---

## 23. Match history and replay — A64-025.5C

### 23.1 A history is scanned for results, and the result was fourth

A row read: avatar, opponent, mode, clock, class — *then* the outcome, then
a reason, then a date, then a "View replay" link. Somebody opening this page
wants to know how their last five games went, and the answer was the fourth
thing on each line.

The outcome leads now, as a fixed-width chip, so results line up down the
left edge and the page can be read without reading it. Same chip the
tournament history uses.

**Won was `--primary`.** A64-025.9 §18.7 gives the brand hue one job —
*interaction* — and a finished result is not one. The profile's own
win/loss/draw bar has been success-red-grey since that phase, so the two
surfaces were colouring the same fact differently. Wins are `--success`
here now; losses were already `--destructive`.

### 23.2 Two tab stops per row, and an inert row

Each row carried a separate "View replay" at its end, so twenty matches were
forty stops — and the row itself did nothing: a player clicked the match
they wanted and nothing happened. `after:inset-0` on the one anchor makes
the whole row the target while leaving exactly **one** link in the
accessibility tree, which is the construction the home page's destination
cards already use. The link keeps its opponent-naming `aria-label`, so
twenty rows are still twenty distinguishable links rather than twenty
"Replay"s.

Dates were `9/4/26`. They are relative now, with the instant on the
element — §21's rule, and for the same reason.

### 23.3 `blitz`, for the fourth time

`Category: blitz` on the replay summary. The raw server enum, in every
locale, on the fourth surface to have shipped one after the tournament card
(§16), the ratings block (§18) and match history (§18.7).

Four occurrences is not four accidents. The shape is always the same: a
value arrives over the wire, somebody renders it because it *reads* like a
word in English, and nothing fails. It is recorded here as a checklist item
for §13's closing audit rather than as a fifth incident report.

### 23.4 The replay had no heading and no way out

The page opened on a board. Its `<h1>` was `sr-only`, which told a screen
reader where it was and nobody else, and a player who arrived from match
history had no route back except the browser's own button. There is a
visible heading and an "All matches" link now.

The move list was the only thing on the page sitting directly on the
background; it is in a card, like the summary beside it.

### 23.5 Measured

| Surface | 1280 light | 360 dark |
| --- | --- | --- |
| `/games/history` | 0 | 0 |
| `/games/{id}/replay` | 0 | 0 |

213 tests pass, `tsc --noEmit` clean, eslint zero errors. No assertion
changed: the row's accessible name is what the suite queries, and it is
unchanged.

---

## 24. Read in Uzbek and Russian — A64-025.5D

Everything from §18 to §23 was designed while looking at English. Three
faults were waiting in the other two languages, and none of them failed
anything.

### 24.1 A chip sized against one language

The match-history result chip was `w-16` — four rems, chosen because it fits
"Won", "Lost" and "Draw". It clips **"Yutqazdingiz"**, **"Natija yo'q"**,
**"Поражение"** and **"Без результата"**, at every width. The profile's
action row was two columns below `sm` for the same reason, and "Ommaviy
profilni ko'rish" does not fit in half of 360.

Both are `min-w` and a stack now. Perfect column alignment across a locale
would need a grid with `display: contents`, which has a history of stripping
list semantics in screen readers — and those semantics were argued for on
purpose, so the trade goes the other way: short labels align, a long one
grows, nothing is cut.

### 24.2 `Intl.RelativeTimeFormat` has no Uzbek

§21 introduced relative timestamps and said they "add no translations to
maintain". Chromium answers
`Intl.RelativeTimeFormat.supportedLocalesOf(["uz"])` with `["uz"]` — and
then renders three hours ago as **`-3 h`** and one day ago as the English
word **"yesterday"**. Partial data, reported as complete, degrading to two
different kinds of wrong in the same list.

Nothing threw. It took a screenshot in Uzbek to see it, two phases after it
shipped.

The sentences are the product's own now, in all three languages, and
`Intl.PluralRules` picks the form — which is the part genuinely worth taking
from the platform, because Russian needs one/few/many and getting that right
by hand is a certainty of getting it wrong. Chromium's plural data for all
three was **checked** before this was written rather than assumed.

Relative now stops at a week and returns `null` beyond it, so the caller
falls back to a date. "Four months ago" is worse than the date it replaces,
and stopping there is also what keeps this to three units rather than six.

`Intl.DateTimeFormat` **does** have Uzbek — the weekday heading reads
"Chorshanba" — so the failure is specific to relative-time patterns and not
a reason to distrust `Intl` generally.

### 24.3 One link that duplicated the header

`/games/history` carried "Back to profile". Match history is a section in
the header at every width, so the button pointed at a route the shell
already offers and made the page read as a sub-page of one profile. Gone,
and the `justify-between` wrapper with it — a row with one child is a row
for nothing.

### 24.4 How they were found, and the check that now exists

Not by reading. A script walks eight surfaces in three languages at two
widths, and reports every leaf element whose `scrollWidth` exceeds its
`clientWidth` — text that is being cut off, whether or not the page
scrolls. Page-level overflow was already 0 everywhere; **that measurement
never sees a clipped label inside a fixed-width box**, which is why five
phases of "0 overflow" missed all of this.

The first run also flagged every `sr-only` element, which clips by design.
The filter for that is part of the check now.

| | uz 360 | uz 1280 | ru 360 | ru 1280 | en 360 | en 1280 |
| --- | --- | --- | --- | --- | --- | --- |
| Before | 2 | 1 | 2 | 2 | 0 | 0 |
| After | 0 | 0 | 0 | 0 | 0 | 0 |

The script is not committed — it is a scratch harness, and a real one
belongs with the visual-regression tooling this repo does not yet have.
Recorded here so the next phase does not have to rediscover the method.

### 24.5 Measured

Eight surfaces × three languages × two widths, zero clipped text. 215 tests
pass (two more than §23: the formatter tests now read the real
dictionaries), `tsc --noEmit` clean, eslint zero errors.

---

## 25. The tournament list, and the browser's Uzbek — A64-025.7B

### 25.1 A list you choose from, not one you read

The card was already a link and already carried the speed colour. What it
did not do was help anybody **decide**, which is the only thing a list of
open tournaments is for.

| Was | Is |
| --- | --- |
| `Entrants: 27 of 32` | the same numbers, over a capacity bar |
| `Entries close September 5, 2026` | `Entries close in 2 days`, and amber inside a day |
| `Created: September 1, 2026` | removed |

"27 of 32" is arithmetic; a bar answers *is this nearly full* without any.
It is `aria-hidden` and the numbers stay beside it, because colour is never
the only signal — and it is coloured **only while registration is open**:
`--warning` from four fifths, `--destructive` when full. A full bar on a
finished tournament is a fact, not a warning, and red would be telling a
reader to hurry about something that ended last week.

`formatRelativeTime` looks forward now as well as back. A deadline is the
same question as a timestamp asked the other way round, and the two
directions genuinely need different words under a minute: something that
recent *just happened*; something that close is *about to*.

The creation date was removed. Nobody picks a tournament by when it was
opened, and it sat at the same weight as the deadline, which is the line a
reader is actually there for. One line to restore if that judgement is
wrong.

### 25.2 The browser does not have Uzbek

§24.2 found that `Intl.RelativeTimeFormat` reports `uz` as supported and has
no patterns for it. That was not one gap. Every `Intl` API this product uses
was then checked in the same browser:

| API | Uzbek in Chromium | Full ICU (Node) | Done |
| --- | --- | --- | --- |
| `RelativeTimeFormat` | `-3 h`, and the English "yesterday" | `3 soat oldin` | our own strings — §24.2 |
| `DateTimeFormat` dates | `2026 M09 3` | `3-sentabr, 2026` | our own month table |
| `DateTimeFormat` weekday | `Thu` | `chorshanba` | our own weekday table |
| `ListFormat` | `Bullet and Yozishma` | `Bullet va Yozishma` | our own conjunction |
| `DateTimeFormat` time | `15:30` | `15:30` | **correct, left alone** |
| `PluralRules` | `one`/`other` | same | **correct, left alone** |
| `NumberFormat` | `1,684.5` | `1 684,5` | **not fixed — see below** |

`M09` is CLDR's *root* month name. The browser resolves the locale, reports
it as supported, and then answers from the fallback data — which is why none
of this failed and none of it was visible in English.

**The number format is knowingly left wrong.** Uzbek groups with a space and
decimates with a comma, and Chromium gives the English form. Unlike `M09` or
an English "and", `1,684.5` is *readable* by an Uzbek speaker — and
hand-writing number formatting means owning percent signs, negatives and
decimal places for three locales, which is the one thing in this table ICU
is genuinely better at. Recorded rather than fixed.

### 25.3 A test that passed while the product was broken

`format.test.ts` asserted `formatDayHeading(…, "uz")` returned
`"Chorshanba"` and passed — because **Vitest runs in Node, which has full
ICU, and the product runs in a browser, which does not.** Green suite,
broken screen, for exactly as long as nobody looked at the product in
Uzbek.

Removing the `Intl` dependency for Uzbek is what makes the suite honest
again: with the values coming from a table in the repository, Node and the
browser cannot disagree. That is the real argument for the tables — larger
than the four defects they fix.

The calendar names live beside the formatter rather than in `locales/`.
Nothing in them is a sentence anybody wrote, and a translator asked to
review "sentabr" would be being asked to check the Gregorian calendar. If
Chromium ever ships the data they can be deleted with no visible change.

### 25.4 Measured

Eight surfaces plus the tournament list, in three languages at 360 and
1280: zero clipped text, zero page overflow. 220 tests pass — seven more
than §23, every one of them a locale the suite could not previously see.
`tsc --noEmit` clean, eslint zero errors.

---

## 26. The page the list links to — A64-025.7C

§25 gave the tournament **list** a capacity bar and a deadline that says
"in 2 days". The page a player reaches by clicking one of those cards still
said `9 of 16` and `Sep 5, 2026, 1:22 PM`. Two views of one tournament,
answering the same question two ways — and the detail page is the one where
somebody actually decides to enter.

| Was | Is |
| --- | --- |
| `Entrants 9 of 16` | the same numbers, over the list's own bar |
| `Registration deadline Sep 5, 2026, 1:22 PM` | `in 1 day` while entries are open, with the instant on the element |
| `Created September 1, 2026` | removed, as it was from the list |
| A borderless ghost "Back to tournaments" | the same control with a chevron, matching the replay's way back |

The entrants row moved to the **end** of the definition list. It was the
first of five, three facts away from the bar that is a picture of it.

### 26.1 The rule §11 wrote down still holds

That section forbade a countdown here, and the reason is good: this client
does not decide whether entries are open, and a ticking number reaching
zero would look like it had. Nothing ticks. `formatRelativeTime` is computed
once at render and is exactly as stale as the status badge beside it — which
is to say, as stale as the fetch. The comment in the code now says which
half of §11 was kept and which was not, rather than reading as though
nobody had noticed it.

### 26.2 Measured

`/tournaments/{id}` in three states — registration open, in progress, and
Uzbek at 360 — with zero clipped text and zero page overflow. 220 tests
pass, `tsc --noEmit` clean, eslint zero errors.

---

## 27. The social surfaces, after the card system — A64-025.8B

`/friends`, `/friends/requests`, `/search`, `/blocked` and `/challenges`
were designed in §17, before the card language, the speed colours and the
rule §18.8 wrote down about destructive weight. They were the last surfaces
still shaped the old way.

### 27.1 Six red words on a list of three friends

§18.8 gave destructive actions destructive **text** and no border — right
for a public profile, which shows one player and two controls. A friends
list shows N players and **2N** of those controls: "Remove friend" and
"Block", in red, on every row. Three friends made six red words, and the
page read as an alarm rather than as a list of people somebody chose to add.

So the rule gains a second half rather than being replaced:

| Tone | Where | Destructive action reads as |
| --- | --- | --- |
| `detail` | one player on their own page | destructive text, as §18.8 |
| `list` | N players down a column | muted, until a cursor or the keyboard reaches it |

Colour was never the only signal in either tone — the label says "Remove
friend" whatever colour it is, and the confirmation dialog is what actually
guards the act. What changes is which of the row's controls a reader sees
first, and on a friends list that should be **Challenge**: the thing
somebody came there to do.

`tone` is an explicit prop rather than a reading of `size`. The two happen
to correlate in today's five call sites and would stop correlating the first
time a dense surface wanted large controls.

### 27.2 Five bordered boxes with gaps between them

Every other list in the product is one card with ruled rows. Social kept a
`<li>` with its own border and a `gap-2` between them, which is the shape
lists had before §18. `PlayerRow` no longer draws a border; the five lists
that render it draw one card around the whole column.

### 27.3 An Uzbek row that lost 123 pixels

`PlayerRow` wraps below `sm`, so the actions were meant to fall to their own
line. `flex-1` let them share the first one instead: at 360 two buttons took
200 of the pixels and the identity kept what was left, so a challenge's meta
line reading `3+2 · Reytingli · 2 soat qoldi` was cut by 123 of them.
`basis-full` below `sm` gives the identity the whole line and pushes the
actions to the next.

English never showed it — "3+2 · Rated · 2h left" fits. That is the third
phase in a row where the fault was only visible in another language, and it
is why the clipping sweep from §24.4 now runs on every surface this epic
touches.

### 27.4 Measured

Five surfaces × three languages × two widths: zero clipped text, zero page
overflow. 220 tests pass, `tsc --noEmit` clean, eslint zero errors.

---

## 28. The game room, read against a live server — A64-025.6D

### 28.1 It could not be read any other way

Every other surface in this epic was reviewed against a mocked API. The game
room has no such surface to mock: it renders from a **WebSocket** snapshot,
behind a ticket, a `room.join` and a `game.resume`. A hand-built socket mock
answered the first three and never produced a snapshot the reducer accepted,
and guessing at payload shapes was costing more than it was worth.

So the room was read against the real thing: Postgres and Redis in Docker,
`apps/api` on 8000, three registered accounts verified through
`app.operator.accounts`, and two browsers driven through the lobby into a
live match.

Three of that setup's constraints are the backend's own and are worth
recording, because each cost a run:

| Symptom | Cause |
| --- | --- |
| The pair would not match a second time | QT-3 excludes a player's most recent opponent — a fixed pair is pairable once. Three accounts, as `tests/e2e/accounts.ts` already documents |
| `/play` showed the waiting card, not the form | A ticket left by the previous run. Leaving clears it; declining an offer would earn the cooldown that then blocks the join |
| `/play` bounced straight to a game | An account still in an unfinished match cannot queue |

### 28.2 One thing that looked wrong and was not

The pieces appeared to sit on both square colours. They do not: reading the
computed background of every occupied cell gives **24 pieces, one colour,
`oklch(0.68 0.055 65)`**. The relief and the cream squares read differently
at different sizes and my eye was wrong. Recorded because measuring instead
of reporting is the only reason a phantom defect did not reach a commit —
the third time this epic that a measurement corrected a reading.

### 28.3 The room never said what was at stake

Two names, two clocks, two ratings, a board — and nowhere did it say whether
the result **counted**. `rated` has been on the snapshot since the room was
built and was kept by nothing, so the one screen where somebody is about to
spend twenty minutes was silent on the one fact they would weigh first.

`GameState` keeps it now and the panel says "Rated" or "Casual" above the
turn line. `null` until the first snapshot, so nothing is guessed before the
server has spoken — §18's rule about invented state.

The time control cannot join it: the snapshot carries the clock's *current*
milliseconds and no initial value, so "3+2" is not something this surface
can say honestly. Left unsaid rather than derived.

### 28.4 `show_coordinates`, the third preference closed

§22.1 recorded five gameplay preferences that were stored and read by
nothing, and closed two. This closes the third. Files and ranks are drawn
inside the board's edge squares, positionally rather than absolutely, so a
flipped board labels its own bottom and left rather than the board's.

They ride `data-coordinates` on the root, like the board theme, so the
component does not ask for a preference it would then thread through two
routes — the replay gets them for the same reason. They are `aria-hidden`:
every square already carries its name in the cell's own label, and reading
"a" again under it would be the same fact twice.

Two of the five remain: `animation_speed` (nothing to speed up until
A64-025.12) and `confirm_move` (a change to move submission, not to
presentation). *Both are closed now — §34.4 and §38. This paragraph is left
as it was written, because it records what was true when this phase shipped
and the two forward pointers say what changed since.*

### 28.5 Measured, and what was not

`/games/{id}` in a live match at 1280: zero clipped text, zero overflow, and
both changes confirmed on screen against a real server.

**The room was not checked at 360 in Uzbek.** Both probe accounts' matches
had ended by the time that run started and the page no longer had a game to
show. Every other surface in this epic has that check; this one does not,
and it is recorded rather than implied.

---

## 29. The front door — A64-025.4B

Five surfaces an anonymous visitor can reach: `/login`, `/register`,
`/forgot-password`, `/reset-password`, `/verify-email`. §11 designed the
forms and they hold up — the brand panel, the gradient action, the field
copy. What did not hold up was the **header above them**.

### 29.1 Four links that all went to the sign-in screen

Every product section is `protectedPage`. So a visitor on `/register` was
offered Play, Tournaments, Friends and Match history, and every one of them
would have bounced them to the screen they were trying to leave.

`pages/home` already refuses to do this, and says why:

> Only for a signed-in player: every destination below is behind the
> verified-email guard, and **offering a card that redirects to sign-in is a
> link that lies about where it goes.**

The home page applied that rule to its own four cards. The header did not
apply it to the same four destinations — and it was breaking it on the one
group of surfaces where *every* visitor is signed out.

`PrimaryNav` and `MobileNav` render nothing when the session is not
authenticated. The signed-out header is the wordmark, the appearance menu
and "Sign in". That is not a truncated product; it is an honest one, and at
360 it also removes a hamburger that opened a panel listing four redirects.

### 29.2 What was checked and left alone

The forms themselves. Six surfaces — the five above plus the not-found
page — in three languages at 360 and 1280: **zero clipped text, zero page
overflow**, every string translated, the brand gradient where §18.7 allows
it and nowhere else.

`/verify-email` renders "The link is incomplete" in a neutral notice rather
than a destructive one. Reading it as informational is defensible — a
visitor who arrived without a token has not done anything wrong — so it was
left as it is rather than reclassified on a hunch.

### 29.3 Measured

| | en | uz | ru |
| --- | --- | --- | --- |
| six surfaces × 360 and 1280 | 0 | 0 | 0 |

220 tests pass, `tsc --noEmit` clean, eslint zero errors.

---

## 30. One email shell for three messages — A64-025.10E

This platform sends three messages. Each built its own HTML:

| Message | Before | After |
| --- | --- | --- |
| Verification code | trilingual, both parts, its own `<div>` | the shared shell |
| Notification | trilingual, both parts, its own `<div>` and button | the shared shell |
| Password reset | **English only, plain text only, an f-string inside the service** | trilingual, both parts, the shared shell |

Three shells meant three places to change a colour and one message that had
been left out of the design entirely. **P2-2 is closed.**

### 30.1 The reset mail already knew the language

`PasswordResetService._deliver` receives a `UserRead`, and `UserRead` has
carried `preferred_language` since A64-012.5. The message was English
because nobody read the field, not because the information was missing —
which is why closing this needed no plumbing, only a template.

The one sentence in that message that does real work is
*"if you did not ask for this, ignore it."* A reset email arriving
unrequested is the first thing somebody sees when an attacker is probing
their account, and the correct advice is genuinely to do nothing — saying so
stops a worried person from clicking the link to "check", which is the one
action that would spend their token for the attacker. That sentence is now
in the reader's own language, which is the whole point of the defect being a
defect.

### 30.2 The brand gradient is deliberately not in email — *revised by §31.2*

`globals.css` gives the product a gradient and §18.7 rations it to three
places. **None of them is an email.** Gradients on a button are unevenly
supported across mail clients, `oklch` is not supported at all, and a client
that drops the background paints white text on white — a button nobody can
read is worse than a plain one.

So the email palette is a flat hex translation of the same brand, and
`layout.py` says so at the top. A divergence that is written down is a
decision; one that is not is drift.

**§31.2 revised this.** The reasoning above is right about a *bare* gradient
and wrong to conclude "never" rather than "never without a solid colour
beneath it". The palette is still flat hex and `oklch` still appears
nowhere; the masthead now carries the ramp over a `bgcolor` fallback.

### 30.3 What each module kept

The shell owns the frame, the button, the code block, the footnote and
**all** the escaping. What stays in each module is the part that is
genuinely its own: which lines a message is made of, in which language.

`notifications/presentation/email/templates.py` had a docstring saying every
user-controlled string is "escaped, once, **here**". It is not here any
more, and the docstring says so — a comment that survives the code it
describes is worse than no comment.

### 30.4 Measured

`ruff`, `mypy --strict` and `pyright` clean across 679 source files. The
notification email's contract test asserts on **content rather than shape**
— it says so in its own docstring — which is why the shell could move
underneath it without a single assertion changing.

### 30.5 Not done

The three messages are the three that exist. §5 lists new email types as out
of scope for this phase and they stay out.

## 31. The email shell, designed — A64-025.10F

§30 made the three messages share one shell. It did not make that shell look
like anything: a bare `<div>`, sans-serif paragraphs, a black button and a
grey line. Every surface a player sees inside the product had been designed
by then, and the first thing a *new* player sees — the verification code —
still looked like process output.

An email is also the one surface this platform renders where it cannot see
the result. So each decision below is stated with the failure it is chosen
against, because there is no browser to check them in.

### 31.1 Why it is built out of tables

§30 described the shell as "a table-free single column" and treated that as
the careful choice. It was the reason the messages could not be designed.
Outlook on Windows renders mail through Word: no flexbox, `max-width` on a
`<div>` ignored, `margin:auto` centring ignored. A centred card with its own
background is **not expressible** without a layout table, so a `<div>`-only
email can never be more than left-aligned text in the client a large share
of recipients read mail in.

Every layout table carries `role="presentation"`. Without it a screen reader
announces "table, three rows" before the first sentence of every message the
platform sends — the accessibility cost of the technique, paid once, here.

### 31.2 The brand gradient, on a solid colour — revising §30.2

§30.2 banned gradients from email outright, because a client that drops one
paints white text on white. That is right about a **bare** gradient. It is
wrong to conclude "never" rather than "never without something solid
beneath it". The masthead declares, in this order:

```html
<td bgcolor="#494fcc" style="background-color:#494fcc;
                             background-image:linear-gradient(115deg,#494fcc,#961a91)">
```

| Client understands | Renders | White text clears |
| --- | --- | --- |
| nothing but attributes | `bgcolor` indigo | 6.41:1 |
| colour, not gradients | `background-color` indigo | 6.41:1 |
| both | the brand ramp | 6.41:1 → 7.36:1 |

All three outcomes satisfy the product-side rule that no text sits over a
gradient whose two ends it has not cleared at 4.5:1 (§18.7). `oklch` still
appears nowhere — these are the same two `globals.css` stops, converted to
sRGB hex once, in `layout.py`.

This is the fourth place the gradient appears, and it is the brand mark of
the product in somebody else's inbox, which is exactly the job §18.7 gives
it.

### 31.3 The preview line

Every mail client shows around ninety characters after the subject in the
message list, taken from the first text in the body. Left alone that is the
greeting — so the most-read line of every message this platform sends was
"Hello Shohruh,".

Each message now supplies its own, hidden in the markup:

| Message | Inbox preview |
| --- | --- |
| Password reset | "Somebody asked to reset the password on your Arena64 account…" |
| Verification code | "Your Arena64 verification code is:" |
| Notification | the notification itself — "Round 3 of Autumn Blitz has been published." |

**Never the code.** A64-021.5H keeps the verification code out of the
subject because a subject is displayed by every notification surface a phone
has — a lock screen, a watch, a preview pane. A preview line is displayed by
the same surfaces, so a preheader carrying the code would put it back
through the other door. `tests/unit/test_email_layout.py` asserts it does
not.

The preheader appears in the HTML part only. In the text part it *is* the
first line, and emitting it there would open every message with its own
second sentence stated twice.

### 31.4 What each message gained

| | |
| --- | --- |
| **Masthead** | The wordmark, as **text**. Mail clients block remote images by default, so a logo is a broken-image icon for most first-time recipients — and the request that would fetch it is a tracking pixel by another name. |
| **Heading** | One line saying what the message is, so the reader is not re-reading the subject to find out. Optional, and the notification emails do not take one: their whole body is a single sentence, and a heading above it would be that sentence twice. |
| **Card** | White on `#f2f2f7`, one hairline border, 14px radius, 560px. A border rather than a shadow, because shadows do not render. |
| **Code panel** | The six digits on a tinted, bordered panel at 30px with 8px of tracking. `text-indent` cancels the trailing letter-space, which otherwise pushes a "centred" code half a space left of centre. |
| **Button** | A table cell that owns the colour, with the anchor filling it. Word collapses the padding on an inline anchor, so the familiar `display:inline-block` button arrives in Outlook as bare underlined text. |
| **Footnote** | Above a hairline, so the small print is separated from the message rather than being its last paragraph. |
| **`lang`** | From the recipient's locale, so a screen reader pronounces a Russian message in Russian. |

Two line heights are stated rather than inherited. The body's is a ratio, so
30px digits would inherit a 48px line box — a code panel a third taller than
the thing in it, with the digits sitting low in the space.

### 31.5 One look, declared

`color-scheme: light`. This design is light and asks clients not to invert
it. A dark variant is honoured by some mail clients and silently ignored by
others, and a half-supported dark mode is worse than one consistent light
one. Adding it later is additive; guessing at it now is not.

### 31.6 What the tests assert, and what they do not

`tests/unit/test_email_layout.py` — ten tests, and deliberately **not** a
snapshot of the markup. A test that pins the exact bytes of a design fails
on every visual change while asserting nothing about whether the design
works.

What it pins is the four things that are contracts rather than appearance:
the code never reaches the preview line; the text part is never escaped and
always carries the bare action URL; the gradient always has a solid colour
declared before it; and every layout table is hidden from assistive
technology. Each was checked by breaking it — removing the `bgcolor`
fallback and letting the preheader into the text part both turn the
corresponding test red.

The messages themselves were reviewed by looking at them: three languages,
both parts, at 560px and at 360px.

### 31.7 Measured

| | |
| --- | --- |
| `ruff` / `mypy --strict` / `pyright` | clean, 680 source files |
| `pytest tests/unit` | 2947 passed, 2 skipped |
| `pytest tests/contract/test_notification_email.py` | 12 passed |
| Assertions changed in existing tests | **none** |

### 31.8 Not done

A dark variant (§31.5). Litmus-style rendering in real clients — every
client-specific decision here is reasoned from a documented rendering
behaviour, not observed, and that is stated so the next person knows which
claims are evidence and which are inference. New email types stay out of
scope, as §5 says.

## 32. The three branches every surface was writing — A64-025.11

§3.9 counted the same loading/failure/empty branches written seventy-four
times. A64-025.2 laid the foundation — `ListState` promoted out of
`features/social`, `Notice` added — and left the sweep to this phase. This is
the sweep.

### 32.1 Six surfaces, six answers to the same three questions

Not a stylistic complaint. This is what was actually on screen:

| Surface | Loading announced as | Skeleton | Failure | Empty |
| --- | --- | --- | --- | --- |
| `ListState` (shared) | `aria-label` on the wrapper | 3 × `h-14` | its own block, `font-medium` | left block, `py-8` |
| `QueryState` (profile) | `aria-label` on the wrapper | `h-24` + two lines | its own block, `min-h-11` | — |
| Notification list | `aria-label` on the wrapper | 3 × `h-16` | `<p role="alert">` in a bare `<div>` | centred dashed panel |
| Tournament history | **nothing — `aria-hidden`** | 2 × `h-16` | **none at all** | dashed paragraph |
| `/games/history` | `sr-only` sentence | 4 × `h-14` | `Notice tone="error"` | left block, `py-8` |
| `/tournaments` | `sr-only` sentence | 3 × `h-24` | its own block, `text-sm` | left block, `py-8` |

Two rows of that table are defects rather than differences.

**The tournament history had no failure branch.** A failed request rendered
nothing, under a heading reading "Tournament history" — which a player reads
as "you have not played in any". A broken list looking exactly like a healthy
empty one is the failure mode the notification list's own docstring warns
about, three files away.

**Its loading state was `aria-hidden`.** A screen reader was told nothing
while it loaded and nothing when it arrived.

That is the argument for a component over a convention: a convention is
forgotten silently, and this is what forgetting it looks like.

### 32.2 What the primitives own now

| Component | Owns |
| --- | --- |
| `LoadFailure` | a sentence, a retry, `Notice tone="error"` — **new** |
| `ListState` | loading, failure (delegated), empty, for a list |
| `QueryState` | loading and failure for a detail read; no empty branch |

`LoadFailure` is new and is the piece that was missing. §3.9 notes that
`apps/admin` has had an `ErrorNotice` since the beginning and `apps/web`,
the larger app, has not. It never shows the error's own text — a player gets
a sentence they can act on and the diagnostic goes to `reportError`
(CLAUDE.md §9.7).

### 32.3 Three things `ListState` had to gain first

Promoting it in .2 was not enough, because three of its decisions were made
for social lists and were wrong elsewhere. Each is why a surface kept writing
its own.

**The skeleton is the caller's shape.** A tournament card is 96px and a match
row is 56px. Three 56px bars in a tournament list's place is not a preview,
it is a different list — and the page jumps when the data lands.
`pendingRows` and `pendingRowClassName` are two props with three demonstrated
callers, not speculation.

**The announcement is the caller's sentence, as real text.** It was an
`aria-label` on an empty `<div role="status">`, which is announced
inconsistently, and it said "Loading…". "Loading tournaments…" is worth more
to somebody who cannot see the skeletons, and those strings already existed
per surface.

**The failure is `LoadFailure`.** Shared with `QueryState`, which is the
branch the six surfaces disagreed about most.

### 32.4 The empty state is a placeholder, not a paragraph

It was a bare left-aligned block. It is now a heading and a sentence,
centred, on a dashed panel that fills the space the list would have filled.

The choice was not taste: the notification list and the tournament history
had **both** independently moved to a dashed panel, which is the product
saying which of the two treatments was right. A left-aligned block collapses
the page to nothing and reads as a sentence that lost its container.

`emptyAction` still appears only where the hint names something to do —
"Find a game" under an empty history, "Search" under an empty friends list,
and nothing under "You are all caught up", because there is nothing to do
but wait.

### 32.5 One retry label, one key

`common.retry`, `state.retry` and `profile.state.retry` were the same
sentence in all three languages. `profile.state.*` was a word-for-word
duplicate of `state.*` throughout. Both are gone; `state.*` is the state
vocabulary and the primitives own it.

The *specific* strings were kept, and that is the opposite decision on
purpose. "Tournaments could not be loaded" tells a player which of the three
lists on their screen is missing; "We could not load this" does not.
Consistency of **shape** is the goal; flattening six specific sentences into
one generic one would make the product worse in its name. The generic
sentence is the fallback, not the rule.

### 32.6 What was deliberately left alone

Consistency is not uniformity, and four surfaces argue for themselves:

| Surface | Why it keeps its own |
| --- | --- |
| `/tournaments/$id` page failure | Tells a 404 from a transient fault and offers the way back to the list. A generic retry cannot. |
| `/games/$id/replay` `Refusal` | Retry only for `unexpected` — a 404 and a refused engine version are stable answers about a permanent record. |
| Queue form's catalogue | Failure and empty are collapsed **on purpose**: the four time controls are seeded by a migration, so an empty catalogue means an unmigrated deployment. Both mean "we cannot start a game right now", which is what it says. |
| Registration panel, challenge dialog | Inline skeletons for a single control, and `role="alert"` for a *mutation* failure. A failed write is not a failed read. |

The two sections of the tournament detail page — standings and bracket — were
writing the same five lines with different spacing, and those did move onto
`LoadFailure`.

### 32.7 Measured

| | Before | After |
| --- | --- | --- |
| Hand-written `role="status"` / `role="alert"` outside `shared/ui` | 65 | 57 |
| Files on `ListState` / `LoadFailure` / `Notice` | 9 | 15 |
| `<Skeleton>` used outside `shared/ui` | 24 | 16 |
| Lists with no failure state | 1 | 0 |
| Duplicate retry-label keys | 3 | 1 |

The remaining 57 are mostly not load states: a game room's turn indicator, a
form field's validation message, a mutation failure announced beside the
control that caused it. Those are `role="status"` doing its actual job, and
`.12` and `.13` will say so rather than counting them again as debt.

| | |
| --- | --- |
| `tsc --noEmit` | clean |
| `eslint` | 0 errors (3 pre-existing `react-refresh` warnings in `shared/realtime/context.tsx`) |
| `vitest` | 225 passed, 33 files |

Five new contract tests, including the one that pins the ordering the
tournament-history fix depends on: a caller computes `isEmpty` from
`entries.length === 0`, which is **true while a request is failing**, so the
failure branch must win. Verified on screen against a live API with the list
reads forced to fail, and against real empty accounts for the empty states.

### 32.8 Not done

`.12` owns motion — the skeletons pulse today with no
`prefers-reduced-motion` guard (P3-5), and that is where it belongs rather
than here. The success-toast half of §3.9 has no surface asking for it yet
and stays out.

## 33. The half of the tree the error boundary could not reach — A64-025.12A

Reported from a running browser, with two screenshots: a bold
**"Something went wrong!"** beside a **"Hide Error"** toggle, and under it, in
red monospace,

```
useTranslation must be used inside an I18nProvider.
```

That panel is TanStack Router's developer default. **It is not this app's
error page**, which is `pages/unexpected-error` and says something a person
can act on.

### 33.1 What was wrong, and it was not the message

`app/providers` wraps everything in one `ErrorBoundary`, and
`createAppRouter` turned the router's own error UI off, with a comment:

> The router's own error UI is deliberately off: errors belong to the one
> boundary in `app/providers`, so there is a single place that reports them
> and a single page a user can be shown.

That was an intention, not a behaviour. **TanStack Router wraps every route
in its own `CatchBoundary`, and a boundary inside the tree catches before one
outside it can.** So a throw from `AppShell` or from any page never reached
`app/providers` at all. The router said so, in the console, every time:

> Warning: The following error wasn't caught by any route! At the very least,
> consider setting an `errorComponent` in your RootRoute!

Two consequences, and the second is worse than the first.

**A user saw a raw error message.** CLAUDE.md §9.7 splits the two audiences:
a user gets a sentence they can act on, an operator gets the detail. That
split was being made in `UnexpectedErrorPage`, which was never rendered.

**Nothing reported it.** `ErrorBoundary.componentDidCatch` calls
`reportError`; it never ran. Every router-level failure this app has ever had
was visible only in the browser it happened in — CLAUDE.md §2.7's silent
failure, in the one place built to prevent it.

### 33.2 The fix

The root route names an `errorComponent`. It reports from an effect — a route
re-renders for reasons that are not a new failure, and reporting from the
render body would send the same error repeatedly — and then renders
`UnexpectedErrorPage`.

`UnexpectedErrorPage` carries hardcoded English rather than translated
strings. That reads like an oversight until it is this page: one of the
throws it must survive is `useTranslation` itself, and an error page that
needs the context that just failed renders a second throw instead of a
message. There is a test for exactly that case.

The comment in `createAppRouter` was corrected rather than left. A comment
describing an intention the code does not have is worse than no comment.

### 33.3 What is still unexplained, stated plainly

**Why `useTranslation` found no provider has not been reproduced, and this
section does not claim it is fixed.**

What the code establishes: `AppShell` is mounted from exactly one place, the
root route, under `I18nProvider`, in a single React root. A `null` context
there cannot mean a missing provider — it means the consumer and the provider
are holding **two different `I18nContext` objects**, which requires two
instances of `shared/i18n/index.tsx`. A production bundle has a static module
graph and cannot do that. A Vite dev server, whose graph is re-fetched with
`?v=` and `?t=` query hashes as modules are invalidated, can.

The report is consistent with that: it appears on returning to a tab that has
been in the background — the window in which the HMR socket reconnects and
applies whatever changed while it was away — and it happened during a session
in which files were being edited continuously.

Three reproduction attempts failed: editing a locale file, editing
`shared/i18n/index.tsx` itself, and doing so before navigating to a
route whose chunk had not yet been imported. Each applied cleanly.

So this is recorded as **open, dev-only, and now observable**. What changed is
that the next occurrence renders the app's own page and calls `reportError`
with `scope: "router"`, rather than printing a stack trace at whoever hit it.

The `react-refresh/only-export-components` rule is off for
`src/shared/i18n/**` and three other paths, with a comment arguing that
splitting a provider from its hook "would make the source worse to read for
no runtime benefit". If this recurs, that comment is the first thing to
re-examine — a context module is the one file where the dev-server
optimisation and the runtime are not independent.

### 33.4 Measured

| | |
| --- | --- |
| `tsc --noEmit` | clean |
| `eslint` | 0 errors, 3 pre-existing warnings |
| `vitest` | 228 passed, 34 files |

Three new tests, and each fails without the fix: the app's page is shown
instead of the router's panel, the error is reported, and the page survives a
failure in the translation context itself. They mount the **real** `rootRoute`
and give it one child that throws, so everything under test is production
code and only the thing that fails is a fixture.

## 34. The motion system — A64-025.12

P3-5 read: *"No `prefers-reduced-motion` handling — correct today, wrong once
motion is added."* Motion has since been added. One surface honoured the
setting — the lobby's waiting card, with a `motion-reduce:animate-none` on
its pulse — and every skeleton, every dialog, every hover transition in the
product ignored it.

### 34.1 One scale, not a duration per component

Everything that moves reads its duration from `--motion-scale`, so there is
one number to change and no component that can be forgotten. That matters
more here than it does for colour: a colour that is missed looks wrong, and
motion that is missed is a migraine trigger for somebody who asked for it to
stop.

| Token | Value | For |
| --- | --- | --- |
| `--duration-fast` | 120 ms × scale | a state change under the pointer — a hover tint, a border |
| `--duration-base` | 200 ms × scale | something entering or leaving |
| `--duration-slow` | 320 ms × scale | where the movement itself is the message |
| `--ease-out` | `cubic-bezier(0.2, 0, 0, 1)` | asymmetric: arrive decelerating, leave at speed |

They are wired to Tailwind's own defaults —
`--default-transition-duration` and `--default-transition-timing-function` —
so **every** `transition-*` utility already in the app inherits them. No
component states a duration, and none can state one that ignores the scale.
`duration-fast` and `duration-slow` are registered as real utilities rather
than written as arbitrary values, because an unknown Tailwind class silently
generates nothing.

### 34.2 Whichever asks for less motion wins

Two sources can ask. The operating system, through `prefers-reduced-motion`,
and the player, through `animation_speed` in their gameplay preferences.

**They are not ranked.** The more conservative of the two applies, which is
why the reduced-motion block is last in the stylesheet and clamps to zero
whatever the attribute said. A player who chose `slow` on a machine set to
reduce motion gets none; moving that block above the attribute blocks would
silently reverse it, so there is a test that fails if anybody does.

`instant` is zero rather than a fourth speed. That is the API's own
contract — *"`animation_speed: instant` disables motion rather than being a
fourth speed — it is an accessibility setting"* — and it is why `instant`
also has to stop the keyframe animations, which read no token.

| | scale | `transition-colors` | `animate-pulse` |
| --- | --- | --- | --- |
| default | 1 | 0.2 s | 2 s, infinite |
| `fast` | 0.6 | 0.12 s | 2 s, infinite |
| `slow` | 1.6 | 0.32 s | 2 s, infinite |
| `instant` | 0 | 0.001 s | **0.001 s, once** |
| OS reduce | 0 | 0.001 s | **0.001 s, once** |
| OS reduce **+** `slow` | 0 | 0.001 s | **0.001 s, once** |

Every figure in that table was read off a running browser, not predicted.

`1 ms` rather than `0 s`, because a zero-length animation never fires
`animationend` in some engines and code waiting on that event waits forever.

### 34.3 The spinner is exempt, and that is not an oversight

The floor stops everything — except one thing.

A spinner's rotation **is** the information: it says "still working". WCAG
2.3.3 exempts motion essential to what a control communicates, and freezing
it leaves a sighted reader with a static icon and no other signal, because
`Spinner`'s label is written for screen readers. A 16px rotation is also far
below the area that triggers vestibular symptoms, which is what the media
query is for. Trading one accessibility win for another is not a win.

Everything else stops. A still skeleton is still a skeleton, and a dialog
that appears without fading is a dialog that appeared.

The stop block is written twice — once in the media query, once for
`[data-motion="instant"]` — because CSS cannot share a declaration block
between a media query and a selector. The alternative is a script that
listens to the media query and writes a third attribute, which puts an
accessibility guarantee behind JavaScript that may not have loaded.

### 34.4 The fourth write-only preference

`animation_speed` has been on `PreferencesResponse` since A64-012.5 and was
read by nothing. A player set "Instant", the form saved it, the server
stored it, and every animation in the product ran exactly as before.

It rides the same mechanism as `board_theme`, `piece_set` and
`show_coordinates`: one data attribute on the root element, and the
stylesheet does the rest. `BoardPreferences` became `GameplayPreferences`
because motion is not a property of the board — the name moved with the
responsibility rather than staying a name that had stopped being true.

**Four of the five gameplay preferences are now honoured.** `confirm_move`
is the one that remains, and it is a rule about submitting a move rather
than anything CSS can express; it stays out of this phase, as §5 says.

That component had **no test at all** before this, including for the three
preferences .5B and .6D closed. Every test in the product would still have
passed if the effect had stopped writing its attributes.

### 34.5 Measured

| | |
| --- | --- |
| `tsc --noEmit` | clean |
| `eslint` | 0 errors, 3 pre-existing warnings |
| `vitest` | 231 passed, 35 files |
| `playwright` (`motion.spec.ts`) | 3 passed, against the built app |

The Playwright suite is where P3-5 is actually closed, and it is there rather
than in jsdom for one reason: **jsdom does not resolve a stylesheet.** A unit
test can assert that `[data-motion="instant"] { --motion-scale: 0 }` is
written; only a browser can say what a `transition-colors` element then
computes to. Removing the reduced-motion block turns all three red.

### 34.6 Not done

No motion was *added*. This phase gives the movement that already existed a
vocabulary and a way to switch it off; a page transition, a board-piece
animation or a list reorder would each be a design decision with its own
argument, and §5's principle 8 — "nothing animates because it can" — is the
reason none of them is in this diff.

## 35. The closing audit — A64-025.13

Nothing here was taken on trust, including this document's own strikethroughs.
Every claim in §4 was checked against the code or measured in a browser, and
three of them were wrong.

### 35.1 Every gate, run

| | |
| --- | --- |
| `ruff check` / `ruff format` | clean, 925 files |
| `mypy --strict` | no issues, 679 source files |
| `pyright` | 0 errors, 0 warnings |
| `pytest tests/unit` | 2947 passed, 2 skipped |
| `lint-imports` | **32 contracts kept, 0 broken** |
| `tsc --noEmit` (web, admin) | clean |
| `eslint` | 0 errors, 3 warnings (`react-refresh` in `shared/realtime/context.tsx`) |
| `prettier --check` | clean |
| `vitest` | 231 passed, 35 files |
| `playwright` (chromium, pwa) | 9 passed |

### 35.2 The original findings, re-checked

| # | This document said | Verdict |
| --- | --- | --- |
| P0-1 | Fixed by .3 | ✅ `features/form-demo` is absent from the tree |
| P1-1 | Fixed by .3 | ✅ **27 screens measured** — 9 routes × uz/ru/en at 360px: 0 clipped elements, 0 page-level overflow. 9 more in dark: 0 |
| P1-2 | *"Future task: A64-025.6"* | ⚠️ **Fixed in code since .6; this table was never updated.** `PlayerSeat` has `LOW_TIME_SECONDS`, a `text-warning` clock and the word beside the colour |
| P1-3 | *"Future task: A64-025.6"* | ⚠️ **Same.** The seats sit above and below the board at every width |
| P2-1…P2-6 | Fixed | ✅ `aria-current` is in four navigation widgets, not zero |
| P3-1 | Fixed | ✅ `Brand` is a `<Link>` with an accessible name, not a `<span>` |
| P3-2 | Fixed | ⚠️ Fixed in the shell — **but the footer still held a literal** (§35.4) |
| P3-3, P3-4, P3-5 | Fixed | ✅ |
| P3-6 | Open, assigned .2 | See below |

**Two P1 entries had been fixed for four phases and still read "future task".**
A defect table that is not updated when the defect closes is a table that
cannot be used to decide what is left — which is the only thing it is for.

**P3-6 is mostly moot, and saying so is more useful than striking it
through.** The finding was "eight primitives; Badge, Select, Tabs, Tooltip,
Dropdown, Switch re-authored per feature". Measured today:

- `shared/ui` holds **12** primitives, not 8.
- Radix's Select, Switch, Tabs, Tooltip, Dropdown and Popover are **not
  installed and not used anywhere**. The three Radix packages present are
  Avatar, Dialog and Slot. The app uses a native `<select>` and a native
  `<input type="checkbox">`, by a decision recorded in `features/privacy` and
  repeated in the notification matrix: they are keyboard-operable, announce
  their own state, and participate in a form for free.
- What remains is **three badge-shaped chips** — a match result, a tournament
  status, a tournament placing — each carrying domain meaning rather than
  being a generic `Badge` written three times.

So there is no consolidation left to do that would not be inventing an
abstraction for three things that only look alike. The finding stays open
in name and is closed in substance.

### 35.3 Sixteen controls below the accessibility minimum

The one product defect this audit found on its own.

Every checkbox in the product was `size-5` — **20 × 20 px**, under WCAG
2.5.8's 24 px minimum — and the label beside it is a 20 px line of text, so
the whole target was 20 px tall. One of them measured **15 px wide**: a flex
child squeezed by a long label, which is the one element in a row that must
not be.

| Surface | Checkboxes | Before | After |
| --- | --- | --- | --- |
| `/settings/notifications` | 12 | 20×20, one 15×20 | 24×24 |
| `/settings/privacy` | 2 | 20×20 | 24×24 |
| `/settings/preferences` | 2 | 20×20 | 24×24 |

24 px is WCAG 2.5.8 (AA). 44 px is 2.5.5 (AAA) and is what `Button`
enforces, but a 44 px tick is the wrong visual for a checkbox — the row's
padding is what gives the aim. The `<select>` controls on the same pages
already measured 44 px and were left alone.

This was invisible to every previous phase because nobody measured. It is
the argument for an audit that runs a browser rather than one that reads.

### 35.4 One concept, two definitions

The footer wrote `Arena64` as a literal while `Brand`, four elements above
it, reads the same string from `layout.title`. Both would have to be found
if the product were ever renamed. Now there is one.

### 35.5 The browser suite had been failing for four phases

`playwright test` was not part of this epic's loop, and it shows.

**A64-025.9B collapsed the header's five account controls into one menu**, so
sign-out stopped being visible until the menu is opened. The e2e suite's
"this session resolved" signal was the sign-out button. **Four specs across
three files failed from that phase onward** — every one on a signal that no
longer exists rather than on the behaviour it covers.

The jsdom suite was updated in the same phase, through `openAccountMenu` in
`shared/test/render`. The browser suite was not, and nothing ran it. It has
the browser twin of that helper now, in one place, so the next header change
is one edit.

`session.ts`'s docstring named `SessionMenu`, which A64-025.3 renamed. Two
phases of drift in eleven lines.

### 35.6 Nine selectors encoding an expectation the product had dropped

`social.spec.ts` and `challenges.spec.ts` selected players by `@username`.
A64-025.8's `PlayerRow` renders the handle line **only when a display name
differs from it** — "alice" beats "alice / @alice", which is two lines saying
one thing. The seeded accounts have no display name, so the line is correctly
absent and those selectors had been matching nothing since that phase.

CLAUDE.md §6.11: either the code is wrong or the test encodes an outdated
requirement, and the change must say which. **The test was outdated.** The
selectors now use the username itself.

### 35.7 What is blocked, and why it is not fixed here

The `lobby` e2e project fails, and seven projects downstream of it do not
run. The cause is **four matches in the local development database, created
2026-09-03 16:22–16:30 UTC**, that are `status = 'active'` with
`time_control_initial_ms` and `clock_turn_started_at` both `NULL`. The clock
adjudicator has nothing to flag, so they never settle, and `resetLobby`
waits ninety seconds for a state that cannot arrive. They involve
`e2e_lobby_one`, `e2e_lobby_two`, `e2e_social_alice`, `e2e_social_bob`,
`e2e_profile_owner` and one human account — they are the residue of the
A64-025.6D investigation, which drove two browser contexts into a live match
and stopped.

**Not fixed here, deliberately.** Clearing them means writing to a database
that holds the owner's own account, which is their call rather than a
change to make inside an audit.

**And it leaves a question worth answering in the backend rather than
guessing at here:** all three columns are nullable, so a match that is
`active` with no clock is a representable state, and nothing will ever end
one. Whether current code can still produce it was not established by this
audit — these rows are a day old and the code has moved. If it can, an
abandoned match is permanent, and a player carrying one cannot queue.

### 35.8 What A64-025 leaves open

| | |
| --- | --- |
| ~~`confirm_move`~~ | **Closed by A64-025.14** — §38. All five gameplay preferences are read now |
| The `useTranslation` context fault | §33.3. Open, dev-only, not reproduced, now reported under `scope: "router"` |
| ~~The stuck-match question~~ | **Answered and fixed** — §36. It was reachable, every unplayed tournament fixture hit it, and the player could not queue again |
| ~~Three `react-refresh` warnings~~ | **Fixed** — §37. They were the rule reporting the §33.3 hazard, and lint is now at zero problems |
| Litmus-style email rendering | §31.8. Every client-specific claim is reasoned, not observed |

Nothing in that list is a surface a player uses being wrong. The epic set out
to make the product look and behave like one thing, and the measurements in
§35.1 and §35.2 are what that claim rests on.

## 36. The match nothing could end — A64-025.13A

§35.7 left a question rather than a finding, because the evidence was four
rows in a development database and the code had moved since they were
written. The question was whether current code could still produce a match
that is `active` with no clock and therefore no way to end.

**It could, and it did so every time a tournament fixture went unplayed.**

### 36.1 What was actually wrong

A tournament fixture is **system-activated**: nobody accepts it, so `game`'s
acceptance expiry never claims it, and it carries **no time control**, so the
clock adjudicator has no deadline to flag. `TournamentNoShowService` is what
was meant to end one. Its composition root says so in as many words:

> Tournament matches are system-activated, so `game`'s acceptance expiry
> never claims one and nothing else would ever end a fixture nobody turned up
> for. This is what does.

It did not. It closed the **attempt** and advanced the **bracket**, and left
the **match** `active` — because there was no port through which it could do
anything else. `game.public` published a command to *create* a match, a read
of its authoritative state, and nothing to *end* one.

The consequence is not cosmetic, and the e2e helper had already written it
down while blaming something else:

> Since A64-020.5A `GET /matches/pending` reports a game that has started,
> and the lobby correctly sends that player to it rather than to the queue
> form — so an account still in yesterday's match cannot join a pool.

**A player adjudicated a no-show could never queue again.** Not until a
deadline that does not exist expired.

### 36.2 An abort, not a win — and this is the whole design

A64-019.5H pinned the old behaviour in a test, with a comment:

> The `game` match is untouched: nothing invented a result.

**That instinct was right and the mechanism was wrong.** Leaving the row
active is not how a fabricated result is avoided; it is how a player is
locked out. And a win would have been worse than cosmetic: these fixtures are
**rated**, so recording one would move two Glicko-2 numbers and write a game
into two players' history that neither played.

So the match ends as `MatchOutcome.NONE` with `TerminationReason.ABORT` —
which the taxonomy already defines for exactly this, in its own words:

| | |
| --- | --- |
| `ABORT` | *"The match ended with no result and no rating effect — MT-11. Not a draw: a draw is an outcome two players played to, an abort is a match that did not happen."* |
| `MatchOutcome.NONE` | *"No result at all. An aborted match, which MT-11 keeps out of every rating and statistic."* |

Nothing is invented. The **walkover stays where it belongs**: an advanced
bracket node with `AdvancementReason.ADJUDICATION`, which is the tournament's
own record of a competitive verdict nobody played to. `game` records only
that the fixture is over.

### 36.3 The port

`game.public.abort` — `MatchAbortUseCase.abort(AbortMatchRequest)`.

**A match id and nothing else.** There is no `reason` parameter because there
is one reason, and a caller that could choose would be a caller that could
record `RESIGNATION` for a game nobody resigned.

**Separate from `commands`.** `GameCommandUseCase` is a participant's
channel — resign, offer, accept, decline — and every one of them is
authorised as "you are in this match". This is a system verdict with no
participant behind it, and folding it into the participant enum would put it
one missing check away from a player-issued one.

**Idempotent, and it has to be.** The sweep re-claims an attempt whose worker
died, so `abort` is called again for a match it already closed. The row lock
is taken *before* the status is read, so two sweeps serialise: the first
closes it, the second reads a completed row and answers `ALREADY_SETTLED`.

**A played game beats a stale sweep.** A result that arrived while the caller
held its claim wins — the same rule `TournamentNoShowService` already applies
to a superseded attempt, enforced on this side of the boundary too so that a
caller which forgot it cannot close a game that was played.

`MatchCompleted` is published with `origin` and `origin_ref`, for the reason
the clock adjudicator records: an aborted match is as much a completion as
one played out. Without the event the tournament's own reconciler keeps
re-reading a match it believes unfinished, and the gateway leaves a room open
on a game that has ended.

### 36.4 The test that had to change, and why that is not weakening it

`test_one_present_player_advances_by_adjudication` asserted
`record.status is ACTIVE`. It now asserts `COMPLETED`, `MatchOutcome.NONE`,
`ABORT` and no winner.

CLAUDE.md §6.11: a failing test means either the code is wrong or the test
encodes an outdated requirement, and the change must say which. **The test
was encoding a proxy.** What it meant to protect — "nothing invented a
result" — is still asserted, and more precisely than before: the outcome is
now named rather than inferred from the absence of one.

### 36.5 Measured

| | |
| --- | --- |
| `ruff` / `mypy --strict` / `pyright` | clean, 681 source files |
| `lint-imports` | 32 contracts kept, 0 broken |
| `pytest tests/unit` | 2952 passed, 2 skipped (2947 before; **+5**) |
| `pytest tests/contract` | 1315 passed, 2 skipped |

The five new unit tests pin the port's own contract — the half a caller
depends on and cannot see: it ends the match without inventing a result, it
publishes the completion, a second call writes nothing, a played game beats
a stale sweep, and an unknown id is reported rather than raised.

The contract suite proves the **sweep** uses it, against a real database and
with the real adjudication rather than a fake — for the reason that suite
already used the real reader: a fake would prove only that the code asks
something.

### 36.6 The four rows

They were closed through `PersistentMatchAbort` itself rather than by hand.
An `UPDATE` would have left the outbox silent and the reconciler re-reading
them forever; the production path publishes `match.completed`, so every
consumer settled them exactly as it would have had the sweep been able to
close them at the time. Their pairing attempts were already `NO_SHOW`, so no
sweep would ever have claimed them again.

`game.match` now holds zero active matches in that database, and the five
accounts involved — three of them the e2e suite's — can queue again.

## 37. Every context in its own module — A64-025.13B

§33.3 recorded a fault it could not reproduce: `useTranslation` throwing
"must be used inside an I18nProvider" under a tree that plainly had one,
after returning to a backgrounded tab. What the code established was that a
`null` context there **cannot** mean a missing provider — `AppShell` is
mounted from one place, under `I18nProvider`, in a single React root — so it
had to mean two `I18nContext` objects, which needs two instances of the
module that creates it.

This is the structural change that makes that impossible in the one place it
was reachable. **It is a precaution on an unreproduced theory and is labelled
as one**, not a proven fix.

### 37.1 The mechanism

React Fast Refresh replaces a module whose exports are all components. A
module that exports a component **and** something else is not refreshable, so
Vite falls back to a full page reload — which is safe.

The dangerous case is the first one applied to a module that also calls
`createContext`. The swap re-runs the module body, `createContext` produces a
**new object**, and the provider renders with it — while every component
already mounted is still reading the old one. Provider present, consumer
`null`, and an error that describes a tree that does not exist.

`react-refresh/only-export-components` is the rule that describes exactly
this shape. The repository had it switched off for four paths, with a comment
arguing that splitting a provider from its hook

> would make the source worse to read for no runtime benefit.

**The second half of that was wrong.** A full reload is a cost. A hot swap of
a context module is a hazard, and the two are not the same thing. The comment
has been corrected rather than deleted.

### 37.2 What moved

| Context | Was | Now |
| --- | --- | --- |
| `I18nContext` | `shared/i18n/index.tsx`, beside `I18nProvider` | `shared/i18n/context.ts`, with `useTranslation` |
| `ThemeContext` | `shared/theme/theme-context.tsx`, beside `ThemeProvider` | `shared/theme/context.ts`, with `useTheme` |
| `RealtimeContext` | `shared/realtime/context.tsx`, beside its provider | `shared/realtime/context.ts`, with its three hooks |

Each new module exports no component, so Fast Refresh will not swap it — a
change there triggers a full reload, which is the safe failure. The providers
keep their own files and their exemption, because a provider holds no
identity that a reload can break.

The three `react-refresh/only-export-components` warnings this repository had
been carrying since before A64-025 were in `shared/realtime/context.tsx`, and
they were the rule reporting this hazard for three years' worth of sessions.
**Lint is now at zero problems**, not zero errors.

### 37.3 What this does not claim

The fault was never reproduced, so nothing here can be called its fix. Three
attempts are listed in §33.3. What can be said precisely:

- The state the error requires — two context objects — needed a module
  instance the dev server could produce and a production bundle could not.
- The one module structure that lets Fast Refresh produce it is gone from
  all three shared contexts.
- If it recurs, `features/auth/model/session-provider` is the remaining
  context module of the same shape, and A64-025.12A's `errorComponent` now
  reports it with `scope: "router"` rather than printing a stack.

`session-provider` was left because it is large, it is a feature module
rather than a shared one, and moving a context out of it is a change to the
authentication path that deserves its own reasoning rather than riding along
with three one-line extractions.

### 37.4 Measured

| | |
| --- | --- |
| `tsc --noEmit` | clean |
| `eslint` | **0 problems** (3 warnings before) |
| `prettier --check` | clean |
| `vitest` | 231 passed, 35 files — no assertion changed |

## 38. The move a player has chosen but not yet played — A64-025.14

> **Renumbered.** This shipped as `A64-026`, which is wrong: the owner's
> roadmap reserves that number for the landing, brand and marketing epic.
> It belongs to A64-025 — it closes the last of the five gameplay
> preferences that redesign phase found unread — so it is `.14`, and the
> landing epic keeps `A64-026`.

`confirm_move` was the last of the five gameplay preferences that nothing
read. A player set it, the form saved it, the server stored it, and every
move still left the browser the instant they clicked a destination.

It is also the only one of the five that could not be closed the way the
other four were. `board_theme`, `piece_set`, `show_coordinates` and
`animation_speed` change how something *looks*, so a data attribute on the
root element and a stylesheet were enough (§34.4). This one changes **when a
move leaves the browser**, which is behaviour, and behaviour needs a step.

### 38.1 The selection model is untouched

`useMoveSelection` still builds a path and still hands back a completed one.
Its docstring argues that a move in draughts is a *prefix*, not a from/to
pair, because two capture sequences can reach the same squares taking
different pieces — and none of that changes.

What changes is **who receives the completed path**. Without the preference
the page submits it, exactly as before. With it, `useMoveConfirmation` holds
it, the board keeps showing it, and a button sends it.

`stage` returns whether it took the move, so the preference is read in one
place rather than branched on in the page and again inside the hook:

```ts
const completed = selection.select(square);
if (completed === null) return;
if (!confirmation.stage(completed)) void submit(completed);
```

### 38.2 The board keeps showing the staged move

`selected={confirmation.staged ?? selection.path}`. Without that, "confirm"
asks a player to confirm something invisible.

The pieces stay where they are — the board renders the server's position,
which has not moved — so what a player sees is the **path they chose**
highlighted, not a preview of the result. That is the honest presentation:
this is the move you are about to play, on the position that is actually
there.

### 38.3 It clears itself, and that is the careful part

A staged move is a claim about a position. The moment the position moves
under it — the opponent played, the game ended, a resync replaced the board
— the path may no longer be legal, and submitting it would be rejected at
best.

`GameState.sequence` is what says the position changed: *"the authoritative
ply, never advanced without a server frame."* It moves for the opponent's
move as much as for this player's, and a resync carries the server's. So the
staged move is dropped whenever it changes, rather than when the hook guesses
that something happened.

Switching the preference off mid-game clears it too, so a staged move is
never left on screen with no control able to answer it.

### 38.4 Two orders, deliberately different

| | |
| --- | --- |
| **DOM** | Play, then Cancel — a keyboard reaches the expected answer first, because the player staged this move on purpose |
| **Screen** | Cancel, then Play — a primary action belongs on the right, beside its cancel |

`flex-row-reverse` is the only thing separating them, which means a refactor
that drops it silently changes the reading order. There is a test that
notices.

`role="status"`, not `alert`: nothing failed, and the board has already
announced each square as it was chosen.

### 38.5 Confirming is not a second chance to change the move

There is no editing. A player who wants a different move cancels and picks
again, which returns them to the selection they already understand. A staged
move that could be adjusted would be a second selection model with its own
rules about multi-capture prefixes, and `useMoveSelection`'s docstring is the
argument for why there is only one.

### 38.6 Measured

| | |
| --- | --- |
| `tsc --noEmit` | clean |
| `eslint` | 0 errors |
| `prettier --check` | clean |
| `vitest` | 238 passed, 36 files (231 before; **+7**) |

The seven tests assert a **negative**, which is what this preference is for:
with it on, choosing a move must *not* submit it. Four of them turn red when
`stage` is made to always decline, which is the whole feature removed.

**The live capture did not happen, and that is stated rather than implied.**
Four attempts against the running stack were each stopped by a different
environment constraint: a disabled submit until a time control is picked,
then session rotation between two browser contexts, then the paired game
flagging on its clock before a move was made, then QT-3 — matchmaking
excludes a player's most recent opponent, which the two seeded accounts had
just become. What *was* confirmed live is that the setting saves and reads
back on, and that two players pair into one room. The staged control itself
is covered by test rather than by photograph.

### 38.7 All five preferences are now read

| Preference | Closed by |
| --- | --- |
| `board_theme` | A64-025.5B |
| `piece_set` | A64-025.5B |
| `show_coordinates` | A64-025.6D |
| `animation_speed` | A64-025.12 |
| `confirm_move` | this task |

None of them was ever missing from the API. Every one had been on
`PreferencesResponse` since A64-012.5 and read by nothing — five settings a
player could change that changed nothing, closed one at a time as each
surface was redesigned.

## 40. The public landing page — A64-026.1

`/` is open, and A64-025.3 kept it that way: an anonymous visitor gets a
signed-out home rather than a redirect. What they got was **one card** — the
wordmark as an `<h1>`, one sentence, two buttons, and two thirds of an empty
screen. Nothing on it said what Arena64 is, what a game costs, whether an
account is needed, or what happens after one.

This is the front door.

### 40.0 Where the design came from

`docs/04-frontend/design-system.md` is a placeholder — every section reads
_TBD_. It is **not** the source of truth and this page did not use it. The
canonical tokens are:

| | |
| --- | --- |
| Values | `apps/web/src/app/styles/globals.css` — `:root`, `.dark`, `@theme inline` |
| Reasoning | §18.7 (colour, and where the gradient is allowed), §34 (motion) |
| Board palette | the same file's `--board-*` and `--piece-*`, shared with the real board |

**No new colour was invented.** Every value on this page is an existing
token: `--primary` for accents and the last-move wash, `--brand-from` /
`--brand-to` for the wordmark, the primary button and the closing panel,
`--card` / `--border` / `--muted` for surfaces, `--speed-*` for the four time
controls, and the board's own variables for the showcase. No hex literal was
written except the white grid in the closing panel's overlay, which sits on
the brand surface and has no token because nothing else needs one.

The gradient's three permitted places (§18.7) are all it takes here: the
wordmark, the primary button, and the closing panel as the brand surface.
Both stops clear 4.5:1 against `--primary-foreground`, which is the rule
that grants them.

### 40.1 What the page answers, in order

What is this → why here → how do I start → what can I do → where do I sign
up. A visitor who reads only the first screen already knows the first,
because the picture beside the headline is a board with pieces on it.

The `<h1>` is a sentence about the product, not the wordmark. The header
carries the wordmark three elements above; a heading repeating it tells a
visitor the name of a thing whose purpose they have not been given.

### 40.2 A marketing header, because the product's does not fit

`AppShell`'s header is *product* navigation — Play, Tournaments, Friends,
Match history — and **every one of those routes is behind
`protectedPage`.** A "Browse tournaments" link here would bounce an
anonymous visitor to `/login`, which is the defect A64-025.3 §2 refused to
ship on the home page.

So the navigation is in-page anchors to the sections that *explain* those
features, and the calls to action go to the account that unlocks them. The
only routes this page links to are `/`, `/login` and `/register` — the three
an anonymous visitor can actually reach. There is a test that walks the
header and fails on a fourth.

Two things the swap must not lose, and does not:

| | |
| --- | --- |
| Appearance and language | `BrowserSettingsMenu` is exported from `widgets/account-menu` and used in **both** headers. One control, not two that drift |
| The `unavailable` rule | Sign-in and register appear only for `status === "anonymous"`. Offering them while the session merely failed tells a signed-in player they were logged out because one request did — the claim that state exists to avoid, and `auth.test.tsx` caught it |

### 40.3 The hero visual is the game room, as a still

`features/game/ui/board.tsx` takes a `GameState`, which comes from
`useGameRoom`, which opens a socket. A landing page is the first request a
visitor makes and the one they judge the product's speed by; pulling the
realtime stack into that bundle to draw a static picture is the trade §26
forbids. The production build confirms it stayed out — `root` and `game` are
separate chunks.

So `widgets/marketing/board-showcase` is a presentation component with no
engine, no socket, no query and no state. It reads the same `--board-*` and
`--piece-*` tokens the real board does, so the picture follows the theme.

**Every piece is on a dark square.** That is not decoration: draughts is
played on one colour, and the first draft put men on light squares — wrong
in a way any player spots before reading a word of the copy. The position is
a plausible mid-game one with a Light king and a last-move highlight, and
the piece table is asserted against the parity rule rather than trusted.

The two seats show a name, a rating and a clock — the four facts
`PlayerSeat` shows, in the same order, so a visitor who signs up recognises
what they were shown. The names are "Light" and "Dark", the sides the domain
itself names: an invented username is the first step towards an invented
rating beside it. The ratings are 1500, which is the platform's genuine
starting value.

### 40.4 Three steps, as an ordered list

"Pick a clock → get paired → play it out" is the lobby's actual flow, and the
order is the content. An `<ol>` rather than a grid of three cards, because a
grid says the same thing to a sighted reader and nothing at all to somebody
using a screen reader. The numerals are `aria-hidden` — the list already
announces the position.

### 40.5 Competitive: copy beside the catalogue

Four facts as a list beside the four time controls as cards, because they
are not peers: the rating is the claim and the rest support it. The clocks
are the most concrete thing on the page and the easiest to check — every one
is a row seeded by the migration that creates the table.

The speed-class colours are written as literals rather than through
`speedAccent`, and the reason is recorded at the call site: that helper maps
a *server-supplied* class and there is no server here, while Tailwind
generates nothing for an interpolated `text-speed-${x}`.

### 40.6 The bracket is drawn, not fetched

§16's whole argument is that the real bracket's connectors come from
`BracketSlot.parent()` — an authoritative relationship, not a CSS
approximation. There is no tournament to read here, so reusing that
component would mean inventing a payload: the fake data §30 forbids, in the
one place it would be easiest to justify.

So `widgets/marketing/bracket-showcase` draws the *shape* — four seats, two
pairings, a final, and the lines between them — with no names, no scores and
no title. The first version used plain grey bars and read as a **loading
skeleton**, which is the failure mode of "honest but empty"; each seat now
carries the piece disc the product uses for a side, so it reads as a bracket
with the names withheld rather than as content that failed to arrive.

### 40.7 Social, and the claim that was not made

Friends, direct challenges, and **quick messages**. The third is the one
worth being careful about: Arena64 has a fixed set of phrases with a spam
rule, and it does not have free-text chat. Copy saying "chat with your
friends" would market a feature this product deliberately does not ship, so
the wording is about saying *good game* without a chat box to police.

### 40.8 One closing statement, not the hero again

A visitor who reached the bottom has read the argument, so the last section
is a statement and one button rather than a second pitch. It is the brand
surface, which is the third place §18.7 grants the gradient.

### 40.9 A footer with only real destinations

There is no privacy page, no terms page, no blog, no Discord and no official
social account, so none of them is linked. A footer column of dead links is
worse than a short footer. The year is computed, because a hardcoded one is
wrong from January.

The four footer links carry `min-h-11`: A64-025.13 §35.3 measured this
product's floor at 44px and found sixteen controls under it, and a row of
17px text links is the same defect in a quieter place. The sweep below
found them at 17px and this is the fix.

### 40.10 `/` is two pages, and the chrome switches with them

| | |
| --- | --- |
| `pages/root` | picks the page — `HomePage` when authenticated, `LandingPage` otherwise |
| `app/router/routes`' `RootLayout` | picks the chrome — `AppShell`, or nothing at `/` when not authenticated |

Two decisions one layer apart, because a page cannot remove a shell it is
rendered inside.

**No flicker, and no redirect.** `/` keeps its guard semantics: an anonymous
visitor is shown a different page, never sent to `/login`, and deep links
elsewhere are untouched. While the session bootstraps, `/` renders the
landing — deliberately, and it is the cheaper of the two mistakes: a
signed-in player sees their own home a moment later, and an anonymous
visitor, who is the majority of this route's traffic, never sees a flash of
product navigation they cannot use.

`pages/home` lost the branches it carried for an anonymous visitor and
gained a type guard instead. It is only ever reached authenticated now.

### 40.11 Metadata, and the parts deliberately left empty

`/` is the landing page, so `index.html`'s title and description **are** its
metadata — this application has no per-route metadata layer, and adding half
of one here would be a second place for it to live. The deeper work is
A64-026.3.

Added: a real title, a real description, `og:type`, `og:site_name`,
`og:title`, `og:description`, `og:image`, `og:locale` and
`twitter:card`. In Uzbek, matching the document's `lang` — that is the value
a crawler and a share preview read, before any script has chosen a locale.

**No `og:url` and no canonical.** Neither is knowable at build time: the
bundle is served from whatever origin deploys it and there is no build-time
origin to interpolate. A wrong canonical tells a crawler to index a page
that is not there, which is worse than none.

**`og:image` is the installed-application icon**, which is a real asset in
this repository. A purpose-built social card is A64-026.2's, and pointing at
one that does not exist yet would ship a broken preview rather than an early
one.

### 40.12 Measured

| | |
| --- | --- |
| `tsc --noEmit` | clean |
| `eslint` | 0 problems |
| `prettier --check` | clean |
| `vitest` | 244 passed, 37 files (238 before; **+6**) |
| `vite build` | passes |

**Ten renderings swept** — 360, 393, 768, 1024, 1280 and 1440, in light and
dark, across uz, ru and en:

| | |
| --- | --- |
| Clipped elements | **0** |
| Page-level horizontal overflow | **0** |
| Interactive targets under 44px | **0** (40 before the footer fix) |
| `<h1>` per page | exactly 1 in all ten |
| Landmarks | `header`, `main`, `footer` present in all ten |

Reduced motion: `--motion-scale` resolves to `0` and a header transition
computes to `0.001s`, so §34's scale covers this page without it declaring
anything of its own. The page adds no animation beyond colour transitions,
which is the honest answer to "hero motion" — a landing page that moves for
its own sake is the template §8 of the brief warns about.

### 40.13 What is deliberately absent

No player count, no games-played counter, no testimonial, no logo wall, no
award, no leaderboard, no pricing, no app-store badge, no community link, no
background video, no cookie banner. §5 forbids inventing a statistic, and a
landing page is where that rule is under the most pressure — "12,481 players
online" is one line of copy away and would be false the moment it was
written.

Every capability named was verified against the repository before it was
written down.

## 41. Brand identity — A64-026.2

The roadmap lists six items for this task. Measured first, three of them
turned out to be already delivered or moot, and one turned out to be a gap
nobody had noticed.

| Roadmap item | Measured state |
| --- | --- |
| Wordmark | Written out **three times** in three files, differing only in size |
| Logo / icon system | `scripts/generate-icons.mjs` exists and works — but drew a **neutral placeholder** |
| Favicon | SVG + three PNGs, same placeholder |
| Social preview asset | **Absent.** `og:image` pointed at the application icon |
| Brand usage rules | Not written down anywhere |
| Final indigo palette | Decided in A64-025.2, in use everywhere — moot |

### 41.1 One wordmark, three sizes

`brand-gradient-text font-semibold tracking-tight` was written out in
`widgets/brand`, `widgets/auth-shell` and the landing page's header. The
drift had already started: the landing header was a step larger than the
other two with nothing recording why.

`widgets/brand` is the definition now — `Brand` for the linked form, and an
exported `wordmarkClass()` for the one caller that must not link, which is
the auth front door: it is already inside a card that is the way home, and a
second link to `/` there would be two controls doing one thing.

### 41.2 The icons were still the placeholder, and said so

`generate-icons.mjs` opened with:

> **A64-025 Product Experience Redesign owns the real brand**; this exists
> so that the manifest is installable and the home-screen tile is not a
> stretched screenshot.

A64-025 decided the brand and never came back. So the installed application
and the browser tab were **the last two surfaces still black and white**
while every other one had been indigo for a phase — a placeholder that
outlived the thing it was waiting for, which is the failure a deferral
comment cannot prevent on its own.

The mark is unchanged in shape and now in the brand palette: the brand
indigo for the light squares, a darker step of the same hue behind them, and
white for the piece — the **only** light element, which is what makes it
read as indigo at 16px rather than as a white tile with a tint.

| | |
| --- | --- |
| White on the deep square | 13.50:1 |
| White on the brand square | 6.15:1 |
| Brand against deep | 2.20:1 — a *shape* boundary, so what matters is visibility at 16px rather than a text threshold |

**No gradient.** §18.7 rations it to three places and none of them is a 16px
square, where a ramp is invisible and only muddies the two tones that have
to separate.

`favicon.svg` is the same description as vector and was edited to match. It
briefly became **invalid XML**, because an XML comment cannot contain a
double hyphen and the token's own name is `--brand-from`; the file is parsed
as part of the check now rather than assumed to be well-formed.

### 41.3 The browser chrome was painting the old page

`theme-color` was `#ffffff` / `#0a0a0a` and the manifest matched it. That is
the shadcn neutral base this product left in A64-025.9 §18.7, when both
themes picked up a trace of the brand hue so a white card reads as a raised
surface. The page moved; the system UI did not.

Both are `--background` now, converted once. The test that pinned the
literal `#0a0a0a` was asserting a copy of a value owned elsewhere — it now
asserts the thing actually worth holding, that **the splash screen and the
browser chrome agree**, since a seam between them is visible on every cold
start. Verified by breaking it.

### 41.4 A social card rendered from the stylesheet

`npm run assets:og` writes `public/og-card.png` at 1200×630.

It uses Playwright rather than the byte-level PNG writer its sibling uses,
and the reason is type: drawing text without a rasteriser means shipping
glyph outlines — a font file, a parser, and a second description of the
wordmark that would drift from the one the application renders. Playwright
is already a dev dependency, and it renders the card from **`globals.css`
itself**, so the tokens, the gradient and the type are the product's. A card
built any other way is a second design system with one consumer.

The output is committed rather than built, because a crawler fetching a
share preview never runs `npm run build` and a `public/` asset is the only
kind it can read.

### 41.5 What still needs one missing piece

`og:image` is a **relative** path, and several crawlers will not resolve
one. Making it absolute needs exactly what `og:url` and the canonical need
— a configured public origin, which nothing in this build has.

All three wait for A64-026.3 together rather than one being guessed at. That
is a single, nameable piece of infrastructure rather than three loose ends,
and it is stated here so the next task starts from it.

### 41.6 Measured

| | |
| --- | --- |
| `tsc --noEmit` | clean |
| `eslint` | 0 errors |
| `prettier --check` | clean |
| `vitest` | 244 passed, 37 files |
| Icons | rendered and read at 16, 32, 192, 180 and maskable-cropped |
| `favicon.svg` | parses as XML |
