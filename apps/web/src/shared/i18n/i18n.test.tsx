import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";

import { I18nProvider, LOCALE_STORAGE_KEY, useTranslation } from "@/shared/i18n";
import en from "@/shared/i18n/locales/en.json";
import ru from "@/shared/i18n/locales/ru.json";
import uz from "@/shared/i18n/locales/uz.json";

/**
 * The two contracts A64-026.5 §44.1 and §44.4 found broken.
 *
 * Both are about what the *public* pages say about themselves, which is
 * why they are asserted rather than left to review: one is announced to
 * assistive technology on every page, and the other is a product claim on
 * five of them.
 */

function Probe() {
  const { setLocale } = useTranslation();
  return (
    <button type="button" onClick={() => setLocale("uz")}>
      switch
    </button>
  );
}

describe("the document language", () => {
  beforeEach(() => {
    document.documentElement.lang = "uz";
    localStorage.clear();
  });

  it("follows the locale that was resolved, not only one that was chosen", () => {
    // The defect: `lang` was written inside `setLocale` only, so it was
    // correct for a player who had switched language and wrong for every
    // player who never touched the control. An English page announced with
    // Uzbek pronunciation rules is WCAG 3.1.1 at level A.
    // Russian, deliberately: jsdom's `navigator.language` is `en-US`, so a
    // test that stored `en` would pass through the fallback and prove
    // nothing about the stored preference.
    localStorage.setItem(LOCALE_STORAGE_KEY, "ru");

    render(
      <I18nProvider>
        <Probe />
      </I18nProvider>,
    );

    expect(document.documentElement.lang).toBe("ru");
  });

  it("falls back to the browser's language when nothing is stored", () => {
    // jsdom reports `en-US`. The document said `uz` regardless, which is
    // the case the defect was actually shipped in — nobody had chosen
    // anything.
    render(
      <I18nProvider>
        <Probe />
      </I18nProvider>,
    );

    expect(document.documentElement.lang).toBe("en");
  });

  it("follows a later switch too", async () => {
    const { default: userEvent } = await import("@testing-library/user-event");
    localStorage.setItem(LOCALE_STORAGE_KEY, "ru");

    render(
      <I18nProvider>
        <Probe />
      </I18nProvider>,
    );
    await userEvent.click(screen.getByRole("button", { name: "switch" }));

    expect(document.documentElement.lang).toBe("uz");
  });
});

describe("what the product says it is", () => {
  it("claims no leaderboard, in any language", () => {
    // `layout.description` is the line under the wordmark on every auth
    // page — sign-in, registration, verification, both password screens.
    // It promised "leaderboards" in all three catalogues, and this
    // application has no leaderboard surface at all: no route, no page, no
    // query. The API has a ladder endpoint behind `CurrentUser`; nothing
    // in this bundle reads it.
    //
    // Asserted by name rather than reviewed, because copy that describes a
    // feature is the kind that outlives the feature's absence.
    for (const [name, messages] of [
      ["en", en],
      ["uz", uz],
      ["ru", ru],
    ] as const) {
      expect(messages.layout.description, name).not.toMatch(
        /leaderboard|liderlar jadval|таблиц[аы] лидеров/i,
      );
    }
  });

  it("calls the game by one name in English", () => {
    // "draughts" everywhere, except this one string, which said
    // "checkers". One product, two names, on the page a visitor reaches
    // straight from the landing page's primary call to action.
    expect(en.layout.description).not.toMatch(/checkers/i);
    expect(en.layout.description).toMatch(/draughts/i);
  });
});
