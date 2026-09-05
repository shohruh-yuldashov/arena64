import { expect, it } from "vitest";

import en from "./locales/en.json";
import ru from "./locales/ru.json";
import uz from "./locales/uz.json";

/**
 * The three locales, as one contract — A64-027A.5 §28.
 *
 * Both failures below shipped in this epic and neither was caught by a
 * rendering test, because a rendering test asserts what a page *shows* and
 * these are about what a page *cannot* show: a value arrives from the
 * database, finds no label, and prints itself. The console then displays
 * `single_elimination` to an operator — the one thing §28 forbids.
 */

/** Every leaf path in a locale, as `a.b.c`. */
function keysOf(value: unknown, prefix = ""): string[] {
  if (typeof value !== "object" || value === null) return [prefix];
  return Object.entries(value).flatMap(([key, child]) =>
    keysOf(child, prefix === "" ? key : `${prefix}.${key}`),
  );
}

it("defines the same keys in every locale", () => {
  // A label added to one file and forgotten in the others is invisible until
  // somebody switches language, and then it prints its own key.
  const english = new Set(keysOf(en));
  for (const [name, locale] of [
    ["uz", uz],
    ["ru", ru],
  ] as const) {
    const keys = new Set(keysOf(locale));
    expect({ [`${name} missing`]: [...english].filter((k) => !keys.has(k)) }).toEqual({
      [`${name} missing`]: [],
    });
    expect({ [`${name} extra`]: [...keys].filter((k) => !english.has(k)) }).toEqual({
      [`${name} extra`]: [],
    });
  }
});

it("labels every tournament format the platform can run", () => {
  // `SUPPORTED_FORMATS` is `{SINGLE_ELIMINATION}` — the only format that
  // reaches the console was the one with no label, so every tournament row
  // printed the enum until A64-027A.5.
  for (const format of ["single_elimination", "round_robin", "swiss"]) {
    expect(en.vocab.tournamentFormat).toHaveProperty(format);
  }
});

it("never leaves a translation empty", () => {
  // An empty string renders as nothing at all, which reads as a missing
  // value rather than a missing translation.
  for (const [name, locale] of [
    ["en", en],
    ["uz", uz],
    ["ru", ru],
  ] as const) {
    const blank = keysOf(locale).filter((key) => {
      const value = key
        .split(".")
        .reduce<unknown>((node, part) => (node as Record<string, unknown>)[part], locale);
      return typeof value === "string" && value.trim() === "";
    });
    expect({ [name]: blank }).toEqual({ [name]: [] });
  }
});
