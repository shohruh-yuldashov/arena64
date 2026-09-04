/**
 * The colour a speed class carries — A64-025.9 §18.7.
 *
 * One hue per class, defined once in `globals.css` and read here as the
 * literal utility strings Tailwind can see. It has to be a lookup of whole
 * class names rather than an interpolated `text-speed-${speedClass}`:
 * Tailwind v4 scans source text for classes, so a constructed name is a
 * class that is never generated and a colour that silently does not appear.
 *
 * Colour is never the only signal. Every caller renders the translated name
 * of the class beside the accent it applies, so the hue is recognition for
 * a returning player and nothing is lost without it (WCAG 1.4.1).
 *
 * `Record<string, …>` and a neutral fallback, for the same reason
 * `speedClassKey` has one: these values arrive over the wire, and a class
 * this build has never seen must render in grey rather than in nothing.
 */
export type SpeedAccent = {
  /** The label naming the class, and small text that belongs to it. */
  text: string;
  /** A rule down the leading edge of a card. */
  border: string;
  /**
   * A wash behind a headline figure, in the class's own hue.
   *
   * The only gradient outside the brand's own four places, and it is here
   * because the figure it sits behind *is* a rating in that class — a
   * profile that led with a purple panel above an orange Blitz card would
   * be giving one fact two colours. The text keeps the card's contrast:
   * a 15% wash is not a background anything is measured against.
   */
  panel: string;
};

const NEUTRAL: SpeedAccent = {
  text: "text-muted-foreground",
  border: "border-l-border",
  panel: "border-border bg-muted/40",
};

const ACCENT: Record<string, SpeedAccent> = {
  bullet: {
    text: "text-speed-bullet",
    border: "border-l-speed-bullet",
    panel: "border-speed-bullet/35 from-speed-bullet/15 bg-gradient-to-br to-transparent",
  },
  blitz: {
    text: "text-speed-blitz",
    border: "border-l-speed-blitz",
    panel: "border-speed-blitz/35 from-speed-blitz/15 bg-gradient-to-br to-transparent",
  },
  rapid: {
    text: "text-speed-rapid",
    border: "border-l-speed-rapid",
    panel: "border-speed-rapid/35 from-speed-rapid/15 bg-gradient-to-br to-transparent",
  },
  classical: {
    text: "text-speed-classical",
    border: "border-l-speed-classical",
    panel: "border-speed-classical/35 from-speed-classical/15 bg-gradient-to-br to-transparent",
  },
  correspondence: {
    text: "text-speed-correspondence",
    border: "border-l-speed-correspondence",
    panel:
      "border-speed-correspondence/35 from-speed-correspondence/15 bg-gradient-to-br to-transparent",
  },
};

export function speedAccent(speedClass: string): SpeedAccent {
  return ACCENT[speedClass] ?? NEUTRAL;
}
