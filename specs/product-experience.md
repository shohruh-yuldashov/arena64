# Feature Specification — Product Experience

| Field | Value |
| --- | --- |
| **Spec ID** | `SPEC-PRODUCT-EXPERIENCE` |
| **Status** | Draft — audit (.1); shell and home (.3); design foundation (.2); authentication (.4) |
| **Owner** | _Unassigned_ |
| **Created** | 2026-08-10 |
| **Last updated** | 2026-08-10 — A64-025.4, the authentication experience |
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
| P2-1 | Bracket has no visual parent-child relationship, though the relationship is authoritative and already on the wire (§3.6) | A64-025.8 |
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
| **A64-025.7** | Profile and social | .2 | Changing privacy rules |
| **A64-025.8** | Tournament and bracket, edges derived from `BracketSlot.parent()` | .2 | Backend contract changes; canvas, zoom or drag |
| **A64-025.9** | Notifications | .2 | Admin notification surfaces |
| **A64-025.10** | Email design system. Fixes P2-2 | .2 (tokens only) | New email types |
| **A64-025.11** | Responsive and mobile polish | .3–.9 | Re-architecting layouts already designed mobile-first |
| **A64-025.12** | Accessibility and motion. Fixes P3-5 | .3–.9 | Adding motion for its own sake |
| **A64-025.13** | Closing audit | all | New work |

---

## 8. Open questions

| # | Question | Blocks |
| --- | --- | --- |
| ~~OQ-1~~ | ~~What does `/` show an anonymous visitor?~~ | **Closed by A64-025.3** — `/` is the authenticated player's product home; the route keeps its existing lack of a guard, so an anonymous visitor gets a signed-out home offering sign-in and registration. A public marketing page is a separate surface for a separate audience and is not this epic's |
| ~~OQ-2~~ | ~~What is Arena64's brand colour?~~ | **Closed by A64-025.2** — indigo, `oklch(0.5 0.19 275)` light and `oklch(0.68 0.16 275)` dark. The reasoning is in §10.1 and it is reversible in two token values |
| OQ-3 | Should the mobile game room pin the clocks above the board, or overlay them on it? | A64-025.6 |
| OQ-4 | Does the bracket keep horizontal scroll on mobile, or switch to a round-at-a-time view below `sm:`? | A64-025.8 |

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
