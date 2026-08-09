import { createContext, type ReactNode, use, useCallback, useMemo, useState } from "react";

import en from "@/shared/i18n/locales/en.json";
import ru from "@/shared/i18n/locales/ru.json";
import uz from "@/shared/i18n/locales/uz.json";

/**
 * Translation for the admin console — A64-024.1 §9.
 *
 * The **same shape** `apps/web/src/shared/i18n` uses: a typed dictionary
 * lookup, keys derived from Uzbek, three locales, no library. Copied rather
 * than imported because AD-04 makes these separate applications and
 * `apps/admin` must not depend on `apps/web` — a shared package is the
 * right answer the day a second thing is shared, and one forty-line
 * lookup is not yet that day.
 *
 * The message trees are deliberately **not** shared either. An admin
 * console's vocabulary is an operator's, not a player's, and merging them
 * would ship every admin string to every player's browser — which is the
 * bundle argument AD-04 already makes about code.
 */
type Messages = typeof uz;

type PathsOf<T> = {
  [K in keyof T & string]: T[K] extends string ? K : `${K}.${PathsOf<T[K]>}`;
}[keyof T & string];

export type TranslationKey = PathsOf<Messages>;

export const LOCALES = ["uz", "ru", "en"] as const;
export type Locale = (typeof LOCALES)[number];

const MESSAGES: Record<Locale, Messages> = { uz, ru, en };
const STORAGE_KEY = "admin.locale";

interface I18nValue {
  locale: Locale;
  setLocale: (locale: Locale) => void;
  t: (key: TranslationKey, values?: Record<string, string | number>) => string;
}

const I18nContext = createContext<I18nValue | null>(null);

function isLocale(value: unknown): value is Locale {
  return typeof value === "string" && (LOCALES as readonly string[]).includes(value);
}

function readStoredLocale(): Locale {
  try {
    const stored = localStorage.getItem(STORAGE_KEY);
    if (isLocale(stored)) return stored;
  } catch {
    /* Storage disabled. The console works; the preference does not persist. */
  }
  const preferred = typeof navigator === "undefined" ? "" : navigator.language.slice(0, 2);
  return isLocale(preferred) ? preferred : "uz";
}

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

  const setLocale = useCallback((next: Locale) => {
    setLocaleState(next);
    document.documentElement.lang = next;
    try {
      localStorage.setItem(STORAGE_KEY, next);
    } catch {
      /* See `readStoredLocale`. */
    }
  }, []);

  const value = useMemo<I18nValue>(() => {
    const messages = MESSAGES[locale];
    return {
      locale,
      setLocale,
      t: (key, values) => interpolate(lookup(messages, key), values),
    };
  }, [locale, setLocale]);

  return <I18nContext value={value}>{children}</I18nContext>;
}

export function useTranslation(): I18nValue {
  const value = use(I18nContext);
  if (value === null) {
    throw new Error("useTranslation must be used inside an I18nProvider.");
  }
  return value;
}
