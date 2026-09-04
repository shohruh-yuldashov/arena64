# Design System

> **Status:** Draft — placeholder
> **Owner:** _Unassigned_
> **Last reviewed:** _Not yet reviewed_

## Purpose

Defines the visual language and shared component contracts used across all frontend applications.

## Scope

Design tokens, component inventory, and accessibility baseline. Excludes component source code.

## Design Principles

_TBD._

## Design Tokens

**The canonical values live in `apps/web/src/app/styles/globals.css`** —
`:root`, `.dark` and the `@theme inline` mapping. They are not restated
here, and must not be: a second copy is the first pair to drift.

The reasoning behind them is recorded where the decisions were made:

| | |
| --- | --- |
| Colour, and where the brand gradient is allowed | `specs/product-experience.md` §18.7 |
| Motion — one scale, two sources, the more conservative wins | `specs/product-experience.md` §34 |
| The board and piece palettes | `globals.css`, `--board-*` / `--piece-*`, with §22 |

## Typography

_TBD._

## Color & Theming

Four token families with disjoint jobs — `--primary` for interaction,
`--brand-from`/`--brand-to` for the Arena64 gradient, `--speed-*` for a
speed class and `--rating` for a personal high. No surface uses more than
two at once. Stated in full in `specs/product-experience.md` §18.7.

## Spacing & Layout

_TBD._

## Brand Usage

**Values are not repeated here.** They live in
`apps/web/src/app/styles/globals.css`; these are the rules for using them,
recorded in A64-026.2 §41.

### The wordmark

Set in the application's own type, in the brand gradient, and defined once —
`apps/web/src/widgets/brand`. Three sizes (`sm`, `base`, `lg`); `Brand` when
it should link home, `wordmarkClass()` when the surrounding element already
is the way home.

Never re-typed inline. Three copies of a treatment is three places to find
when the brand changes, and the drift had already started before this rule
existed.

### The mark

A 2×2 draughts board with one piece: the smallest thing that is recognisably
this product rather than a generic tile, and it survives 16px, which is
where a favicon lives.

| | |
| --- | --- |
| Light squares | the brand indigo |
| Deep squares | a darker step of the same hue |
| The piece | white, and the **only** light element — which is what makes the mark read as indigo at every size |

Generated, not hand-drawn: `npm run assets:icons` writes the four PNGs and
`public/icons/favicon.svg` is the same description as vector. The two are
one artwork in two forms and must be edited together.

**No gradient on the mark.** §18.7 rations the gradient to three places and
none of them is a 16px square, where a ramp is invisible and only muddies
the two tones that have to separate.

### The gradient

Three places, and the list is closed — `specs/product-experience.md` §18.7:
the wordmark, the primary button, and a brand surface. A fourth needs that
section changed first, and both ends of the ramp must clear 4.5:1 against
whatever text sits on them.

### The social card

`npm run assets:og` renders `public/og-card.png` from this application's own
stylesheet, so the card cannot drift from the design system: it *is* the
design system, screenshotted.

### Theme colours

`theme-color` in `index.html` and the manifest's `theme_color` /
`background_color` are `--background` in each theme, not a neutral
approximation of it. A test asserts the manifest and the document agree,
because a seam between the splash screen and the browser chrome is visible
on every cold start.

## Component Inventory

_TBD._

## Accessibility Baseline

_TBD._

## Motion Guidelines

_TBD._

## TODO

- [ ] Assign a document owner
- [ ] Draft the sections above
- [ ] Link related decision records in `docs/07-decisions/`
- [ ] Review and promote status from Draft to Approved
