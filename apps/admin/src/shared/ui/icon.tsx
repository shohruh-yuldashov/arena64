import type { SVGProps } from "react";

/**
 * The console's icon set — A64-027A §27.
 *
 * ## Why not a library
 *
 * The console needs about twenty glyphs. `lucide-react` would supply them
 * and a runtime, and this app's entire dependency list is React and a
 * router — the same argument that kept a charting library out of
 * `analytics` (§36 of that task) applies here at the same scale. Twenty
 * paths inlined here cost nothing to load and nothing to keep current.
 *
 * One set, one stroke weight, one grid. Mixing packs is the failure §27
 * names, and it is visible immediately: two icons beside each other drawn
 * at different weights read as a bug.
 *
 * ## Accessibility
 *
 * Every icon here is `aria-hidden`. An icon is decoration beside a label,
 * never the label itself — §27 requires primary controls to carry text, and
 * a name announced twice is worse than an icon announced once. Where a
 * control genuinely has no visible text, the *control* carries the
 * accessible name, not the glyph.
 */

export type IconName =
  | "dashboard"
  | "users"
  | "matches"
  | "tournaments"
  | "notifications"
  | "analytics"
  | "moderation"
  | "audit"
  | "settings"
  | "search"
  | "filter"
  | "send"
  | "edit"
  | "trash"
  | "warning"
  | "success"
  | "info"
  | "chevronRight"
  | "chevronDown"
  | "close"
  | "menu"
  | "panel"
  | "contrast"
  | "sun"
  | "moon"
  | "signOut"
  | "external"
  | "refresh";

/**
 * 24×24, stroked, `currentColor`. Stroke rather than fill so one glyph
 * works on every surface the console has, and `currentColor` so an icon
 * inside a destructive control is destructive without a second variant.
 */
const PATHS: Record<IconName, string> = {
  dashboard: "M4 13h6V4H4v9Zm0 7h6v-5H4v5Zm10 0h6v-9h-6v9Zm0-16v5h6V4h-6Z",
  users:
    "M16 20v-1a4 4 0 0 0-4-4H7a4 4 0 0 0-4 4v1M9.5 11a3.5 3.5 0 1 0 0-7 3.5 3.5 0 0 0 0 7Zm11.5 9v-1a4 4 0 0 0-3-3.9M16 4.1a4 4 0 0 1 0 7.8",
  matches: "M3 7h18M3 12h18M3 17h18M8 4v16M16 4v16",
  tournaments:
    "M7 4h10v4a5 5 0 0 1-10 0V4Zm10 1h3v2a3 3 0 0 1-3 3M7 5H4v2a3 3 0 0 0 3 3m5 3v4m-3 3h6",
  notifications: "M18 9a6 6 0 1 0-12 0c0 5-2 6-2 6h16s-2-1-2-6M13.7 20a2 2 0 0 1-3.4 0",
  analytics: "M4 20V10m5 10V4m5 16v-7m5 7V8",
  moderation: "M12 3 4 6v6c0 4.5 3.2 8.3 8 9 4.8-.7 8-4.5 8-9V6l-8-3Zm0 6v4m0 3h.01",
  audit:
    "M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8l-5-5Zm0 0v5h5M9 13h6m-6 4h4",
  settings:
    "M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6Zm7.4-3a7.4 7.4 0 0 0-.1-1.1l2-1.6-2-3.4-2.4 1a7.4 7.4 0 0 0-1.9-1.1L14.6 3H9.4l-.4 2.8a7.4 7.4 0 0 0-1.9 1.1l-2.4-1-2 3.4 2 1.6a7.4 7.4 0 0 0 0 2.2l-2 1.6 2 3.4 2.4-1a7.4 7.4 0 0 0 1.9 1.1l.4 2.8h5.2l.4-2.8a7.4 7.4 0 0 0 1.9-1.1l2.4 1 2-3.4-2-1.6c.06-.36.1-.73.1-1.1Z",
  search: "M11 19a8 8 0 1 0 0-16 8 8 0 0 0 0 16Zm10 2-4.35-4.35",
  filter: "M3 5h18l-7 8v6l-4 2v-8L3 5Z",
  send: "M21 3 3 10.5l7 3 3 7L21 3Zm0 0-11 11",
  edit: "M12 20h9M16.5 3.5a2.1 2.1 0 0 1 3 3L8 18l-4 1 1-4 11.5-11.5Z",
  trash: "M3 6h18M8 6V4h8v2m1 0v14a2 2 0 0 1-2 2H9a2 2 0 0 1-2-2V6h10ZM10 11v6m4-6v6",
  warning: "M12 3 2 20h20L12 3Zm0 6v5m0 3h.01",
  success: "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18Zm-4-9 3 3 5-5",
  info: "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18Zm0-5v-5m0-3h.01",
  chevronRight: "m9 5 7 7-7 7",
  chevronDown: "m5 9 7 7 7-7",
  close: "M18 6 6 18M6 6l12 12",
  menu: "M3 6h18M3 12h18M3 18h18",
  panel: "M4 4h16a1 1 0 0 1 1 1v14a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V5a1 1 0 0 1 1-1Zm5.5 0v16",
  contrast: "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18Zm0-18v18",
  sun: "M12 17a5 5 0 1 0 0-10 5 5 0 0 0 0 10Zm0-14v2m0 18v-2M5 5l1.5 1.5M17.5 17.5 19 19M3 12h2m14 0h2M5 19l1.5-1.5M17.5 6.5 19 5",
  moon: "M21 13A9 9 0 1 1 11 3a7 7 0 0 0 10 10Z",
  signOut: "M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4m7 14 5-5-5-5m5 5H9",
  external: "M14 4h6v6m0-6L10 14M18 14v4a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4",
  refresh: "M21 12a9 9 0 1 1-2.6-6.4M21 4v5h-5",
};

export interface IconProps extends Omit<SVGProps<SVGSVGElement>, "name"> {
  name: IconName;
  /** Rendered size in pixels. The stroke scales with it. */
  size?: number;
}

export function Icon({ name, size = 18, className, ...rest }: IconProps) {
  return (
    <svg
      className={className === undefined ? "icon" : `icon ${className}`}
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.75}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
      {...rest}
    >
      <path d={PATHS[name]} />
    </svg>
  );
}
