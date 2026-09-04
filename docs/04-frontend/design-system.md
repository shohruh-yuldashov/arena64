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
