import { type ReactNode, useCallback, useEffect, useMemo, useState } from "react";

import { I18nContext, type I18nContextValue } from "@/shared/i18n/context";
import en from "@/shared/i18n/locales/en.json";
import ru from "@/shared/i18n/locales/ru.json";
import uz from "@/shared/i18n/locales/uz.json";

/**
 * Translation, as a typed dictionary lookup.
 *
 * ## Why this and not a library — A64-020.2, closing OQ-2 partially
 *
 * `next-intl` did not survive the move to Vite (ADR-002), and the three
 * locale files have been sitting unwired since. This phase needs
 * translated auth strings, so it wires them — with the smallest thing that
 * does the job.
 *
 * What that job actually is: look up a dotted key, interpolate a few named
 * values, and switch language. Roughly forty lines. Adding `i18next` and
 * its React bindings for that would be a dependency, a plugin chain and an
 * initialisation order to get right, bought against a need nothing has
 * demonstrated. **When it stops being right** is a measurement, not a
 * guess: the first plural, the first date or number that must be formatted
 * per locale, or the first namespace that has to be loaded lazily. Those
 * are ICU's problems and it is worth taking ICU to solve them — recorded
 * in `specs/frontend.md` OQ-2, which stays open for that reason.
 *
 * ## Types come from Uzbek
 *
 * `TranslationKey` is derived from `uz.json`, so a key that exists in one
 * file and not another is a **compile error** rather than a string that
 * renders as itself in production. Uzbek is the source because it is the
 * product's primary language; the other two are checked against it below.
 */
type Messages = typeof uz;

/** Every dotted path in the message tree, as a union. */
type PathsOf<T> = {
  [K in keyof T & string]: T[K] extends string ? K : `${K}.${PathsOf<T[K]>}`;
}[keyof T & string];

export type TranslationKey = PathsOf<Messages>;

export const LOCALES = ["uz", "ru", "en"] as const;
export type Locale = (typeof LOCALES)[number];

//: The other two are typed as `Messages`, so a missing or misspelled key
//: in either fails `npm run typecheck` rather than shipping.
const MESSAGES: Record<Locale, Messages> = { uz, ru, en };

export const LOCALE_STORAGE_KEY = "locale";

const LOCALE_NAMES: Record<Locale, string> = {
  uz: "O'zbekcha",
  ru: "Русский",
  en: "English",
};

function isLocale(value: unknown): value is Locale {
  return typeof value === "string" && (LOCALES as readonly string[]).includes(value);
}

function readStoredLocale(): Locale {
  try {
    const stored = localStorage.getItem(LOCALE_STORAGE_KEY);
    if (isLocale(stored)) return stored;
  } catch {
    /* Storage disabled. The app works; the preference does not persist. */
  }
  // The browser's preference, then Uzbek. Not English: this product's
  // primary audience reads Uzbek, and defaulting to English would put the
  // majority of players in their second language by default.
  const preferred = typeof navigator === "undefined" ? "" : navigator.language.slice(0, 2);
  return isLocale(preferred) ? preferred : "uz";
}

/** Walks a dotted path. Returns the key itself when it does not resolve. */
function lookup(messages: Messages, key: string): string {
  const found = key
    .split(".")
    .reduce<unknown>(
      (node, part) =>
        typeof node === "object" && node !== null
          ? (node as Record<string, unknown>)[part]
          : undefined,
      messages,
    );
  // Returning the key rather than an empty string: a visible `auth.login.title`
  // in the UI is a bug report; a blank space is a mystery. Unreachable
  // while `TranslationKey` is derived from the message tree, and kept
  // because JSON edited by hand can outrun the types for one commit.
  return typeof found === "string" ? found : key;
}

function interpolate(template: string, values?: Record<string, string | number>): string {
  if (values === undefined) return template;
  return template.replace(/\{(\w+)\}/g, (match, name: string) =>
    name in values ? String(values[name]) : match,
  );
}

export function I18nProvider({ children }: { children: ReactNode }) {
  const [locale, setLocaleState] = useState<Locale>(readStoredLocale);

  // A64-026.5 §44.1. `index.html` ships `lang="uz"`, which is right before
  // any script has run and wrong the moment this provider resolves
  // something else — a stored preference, or a browser asking for Russian.
  //
  // It used to be written only inside `setLocale`, so the attribute was
  // correct for a player who had *switched* language and wrong for every
  // player who never touched the control: an English page announced with
  // Uzbek pronunciation rules, which is WCAG 3.1.1 at level A.
  //
  // An effect rather than a line in render, because writing to the
  // document during render is a side effect in the middle of one.
  useEffect(() => {
    document.documentElement.lang = locale;
  }, [locale]);

  const setLocale = useCallback((next: Locale) => {
    setLocaleState(next);
    try {
      localStorage.setItem(LOCALE_STORAGE_KEY, next);
    } catch {
      /* See `readStoredLocale`. */
    }
  }, []);

  const value = useMemo<I18nContextValue>(() => {
    const messages = MESSAGES[locale];
    return {
      locale,
      setLocale,
      t: (key, values) => interpolate(lookup(messages, key), values),
      localeName: (candidate) => LOCALE_NAMES[candidate],
    };
  }, [locale, setLocale]);

  return <I18nContext value={value}>{children}</I18nContext>;
}

// A64-025.13B §37. The context and its hook live in `context.ts`, which
// defines no component — see that module on why that matters at runtime.
export { type I18nContextValue, useTranslation } from "@/shared/i18n/context";
