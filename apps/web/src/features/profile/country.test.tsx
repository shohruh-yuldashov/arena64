import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { httpClient } from "@/shared/api/client";
import { env } from "@/shared/config/env";
import { LOCALE_STORAGE_KEY } from "@/shared/i18n";
import { mswServer } from "@/shared/test/msw/server";
import { openAccountMenu, renderApp } from "@/shared/test/render";

/**
 * The country picker and the country on a profile — A64-031.F.
 *
 * Through the real app, as `profile.test.tsx` is: the router, the form, the
 * Axios instance and MSW at the network. What is asserted about saving is
 * the body that reaches `PATCH /profile`, because that is the only thing the
 * backend ever sees of this picker.
 *
 * The backend half — which codes are accepted, upper-casing, `null`
 * clearing — is `apps/api/tests/contract/test_profile_editing_api.py`.
 */
const url = (path: string) => `${env.VITE_API_URL}${path}`;
const REFRESH = url("/auth/browser/refresh");
const ME = url("/profile/me");
const PATCH_PROFILE = url("/profile");

const PLAYER_ID = "019fb9ea-0a0c-7cec-9c5f-402727c31a96";

function envelope<T>(data: T) {
  return { data, meta: { request_id: null, correlation_id: null } };
}

function myProfile(country: string | null) {
  return envelope({
    id: PLAYER_ID,
    username: "player_one",
    display_name: "Player One",
    bio: null,
    country,
    avatar_url: null,
    thumbnail_url: null,
    joined_at: "2026-01-15T10:00:00Z",
    is_online: true,
    last_seen: null,
    statistics: null,
  });
}

/**
 * Signed in, holding `country`, with every `PATCH` recorded.
 *
 * The answer echoes what was sent, upper-cased the way the server stores
 * it, so the form re-baselines against a realistic response.
 */
function signedInWith(country: string | null) {
  const patches: Record<string, unknown>[] = [];
  mswServer.use(
    http.post(REFRESH, () =>
      HttpResponse.json(
        envelope({
          access_token: "token-1",
          token_type: "Bearer",
          expires_in: 900,
          user: {
            id: PLAYER_ID,
            username: "player_one",
            display_name: "Player One",
            email: "player@example.com",
            is_active: true,
            is_verified: true,
          },
        }),
      ),
    ),
    http.get(ME, () => HttpResponse.json(myProfile(country))),
    http.patch(PATCH_PROFILE, async ({ request }) => {
      const body = (await request.json()) as Record<string, unknown>;
      patches.push(body);
      return HttpResponse.json(myProfile((body.country as string | null) ?? null));
    }),
  );
  return patches;
}

function publicProfile(username: string, country: string | null) {
  mswServer.use(
    http.post(REFRESH, () =>
      HttpResponse.json(
        {
          code: "invalid_session",
          message: "No session.",
          request_id: null,
          correlation_id: null,
        },
        { status: 401 },
      ),
    ),
    http.get(url(`/profiles/${username}`), () =>
      HttpResponse.json(
        envelope({
          id: PLAYER_ID,
          username,
          display_name: "Player One",
          avatar_url: null,
          thumbnail_url: null,
          country,
          language: "uz",
          bio: null,
          joined_at: "2026-02-01T10:00:00Z",
          statistics: null,
          ratings: { classic: null, rapid: null, blitz: null },
        }),
      ),
    ),
  );
}

const countryButton = (name: RegExp) => screen.findByRole("button", { name });
const saveButton = () => screen.getByRole("button", { name: /^save$|^saqlash$|^сохранить$/i });
const search = () =>
  screen.getByRole("combobox", {
    name: /^search countries$|^mamlakatni qidirish$|^поиск страны$/i,
  });

beforeEach(() => {
  vi.spyOn(console, "error").mockImplementation(() => {});
  httpClient.interceptors.request.clear();
  httpClient.interceptors.response.clear();
  localStorage.clear();
  document.documentElement.lang = "uz";
});

describe("choosing a country", () => {
  it("searches, chooses with the keyboard alone, and saves the code", async () => {
    const user = userEvent.setup();
    const patches = signedInWith("UZ");
    renderApp({ path: "/settings/profile" });

    const trigger = await countryButton(/^country uzbekistan$/i);
    expect(trigger).toHaveTextContent("🇺🇿");

    trigger.focus();
    await user.keyboard("{Enter}");

    // The search box has focus the moment the dialog opens.
    const dialog = await screen.findByRole("dialog", { name: "Country" });
    expect(search()).toHaveFocus();

    await user.keyboard("united");
    const options = within(dialog).getAllByRole("option");
    expect(options.map((option) => option.textContent)).toEqual([
      "🇦🇪United Arab Emirates",
      "🇬🇧United Kingdom",
      "🇺🇸United States",
    ]);
    // The first match is highlighted; the arrow moves the highlight and
    // leaves focus in the search box, as the combobox pattern requires.
    expect(search()).toHaveAttribute("aria-activedescendant", options[0]?.id);
    await user.keyboard("{ArrowDown}");
    expect(search()).toHaveAttribute("aria-activedescendant", options[1]?.id);
    expect(options[1]).toHaveAttribute("aria-selected", "true");
    expect(options[0]).toHaveAttribute("aria-selected", "false");
    expect(search()).toHaveFocus();

    await user.keyboard("{Enter}");

    // Closed, focus back on the trigger, and the trigger names the choice.
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    const chosen = screen.getByRole("button", { name: /^country united kingdom$/i });
    expect(chosen).toHaveFocus();
    // Choosing is not saving.
    expect(patches).toHaveLength(0);

    await user.click(saveButton());

    await waitFor(() => expect(patches).toHaveLength(1));
    expect(patches[0]?.country).toBe("GB");
    expect(await screen.findByRole("status")).toHaveTextContent(/saved/i);
    await waitFor(() => expect(saveButton()).toBeDisabled());
  });

  it("chooses with a tap", async () => {
    const user = userEvent.setup();
    const patches = signedInWith("UZ");
    renderApp({ path: "/settings/profile" });

    await user.click(await countryButton(/^country uzbekistan$/i));
    await user.type(search(), "kaz");
    await user.click(screen.getByRole("option", { name: /kazakhstan/i }));
    await user.click(saveButton());

    await waitFor(() => expect(patches[0]?.country).toBe("KZ"));
  });

  it("opens on the saved country", async () => {
    const user = userEvent.setup();
    signedInWith("UZ");
    renderApp({ path: "/settings/profile" });

    await user.click(await countryButton(/^country uzbekistan$/i));

    const highlighted = document.getElementById(
      search().getAttribute("aria-activedescendant") ?? "",
    );
    expect(highlighted).toHaveTextContent("Uzbekistan");
    expect(highlighted).toHaveAttribute("aria-selected", "true");
  });

  it("removes a country, which the API spells null", async () => {
    const user = userEvent.setup();
    const patches = signedInWith("UZ");
    renderApp({ path: "/settings/profile" });

    await user.click(await countryButton(/^country uzbekistan$/i));
    // Offered first, as an option like any other.
    expect(screen.getAllByRole("option")[0]).toHaveTextContent("Remove country");
    await user.click(screen.getByRole("option", { name: "Remove country" }));

    expect(await countryButton(/^country not set$/i)).toBeVisible();
    await user.click(saveButton());

    await waitFor(() => expect(patches).toHaveLength(1));
    expect(patches[0]?.country).toBeNull();
  });

  it("leaves no country as no country, whatever the browser's language", async () => {
    // An Uzbek interface on an Uzbek browser is still not a country — the
    // player has not chosen one, and nothing here chooses for them.
    vi.spyOn(navigator, "language", "get").mockReturnValue("uz-UZ");
    const user = userEvent.setup();
    const patches = signedInWith(null);
    renderApp({ path: "/settings/profile" });

    const trigger = await countryButton(/^mamlakat tanlanmagan$/i);
    await user.click(trigger);
    // Nothing to remove, and nothing highlighted as if it had been chosen.
    expect(screen.queryByRole("option", { name: /olib tashlash/i })).not.toBeInTheDocument();
    expect(screen.getAllByRole("option")[0]).toHaveTextContent("Afgʻoniston");
    await user.keyboard("{Escape}");

    await user.type(screen.getByLabelText(/ko'rsatiladigan ism/i), " II");
    await user.click(saveButton());

    await waitFor(() => expect(patches).toHaveLength(1));
    expect(patches[0]?.country).toBeNull();
  });

  it("says when nothing matches, and Enter then neither chooses nor submits", async () => {
    const user = userEvent.setup();
    const patches = signedInWith("UZ");
    renderApp({ path: "/settings/profile" });

    await user.click(await countryButton(/^country uzbekistan$/i));
    await user.type(search(), "atlantis{Enter}");

    expect(screen.queryAllByRole("option")).toHaveLength(0);
    expect(screen.getByText("No country matches “atlantis”")).toBeVisible();
    expect(search()).not.toHaveAttribute("aria-activedescendant");
    expect(screen.getByRole("dialog")).toBeVisible();
    expect(patches).toHaveLength(0);
  });

  it("closes on Escape without changing anything", async () => {
    const user = userEvent.setup();
    signedInWith("UZ");
    renderApp({ path: "/settings/profile" });

    const trigger = await countryButton(/^country uzbekistan$/i);
    await user.click(trigger);
    await user.keyboard("{ArrowDown}{ArrowDown}{Escape}");

    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(trigger).toHaveAccessibleName(/^country uzbekistan$/i);
    expect(trigger).toHaveFocus();
    expect(saveButton()).toBeDisabled();
  });

  it("names a stored code that is no longer valid, and will not send it back", async () => {
    // `XX` was accepted by A64-012.1 and is rejected since A64-012.3, so a
    // profile can hold it. Re-sending it would only earn a 422.
    const user = userEvent.setup();
    const patches = signedInWith("XX");
    renderApp({ path: "/settings/profile" });

    const trigger = await countryButton(/^country xx$/i);
    expect(trigger).toHaveAccessibleDescription(/“XX” is not a country we recognise/);

    await user.type(screen.getByLabelText(/display name/i), " II");
    await user.click(saveButton());

    expect(await screen.findByText("Choose a country from the list")).toBeVisible();
    expect(trigger).toHaveAttribute("aria-invalid", "true");
    expect(patches).toHaveLength(0);

    await user.click(trigger);
    await user.type(search(), "uzbek{Enter}");
    await user.click(saveButton());

    await waitFor(() => expect(patches).toHaveLength(1));
    expect(patches[0]?.country).toBe("UZ");
  });

  it("keeps the choice when the save fails, so it can be retried", async () => {
    const user = userEvent.setup();
    signedInWith("UZ");
    mswServer.use(
      http.patch(PATCH_PROFILE, () =>
        HttpResponse.json(
          {
            code: "validation_error",
            message: "Country must be an ISO code.",
            request_id: null,
            correlation_id: null,
          },
          { status: 422 },
        ),
      ),
    );
    renderApp({ path: "/settings/profile" });

    await user.click(await countryButton(/^country uzbekistan$/i));
    await user.type(search(), "kaz{Enter}");
    await user.click(saveButton());

    expect(await screen.findByText("Check what you entered.")).toBeVisible();
    // The server's English prose is for operators, not the page.
    expect(screen.queryByText("Country must be an ISO code.")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^country kazakhstan$/i })).toBeVisible();
    expect(saveButton()).toBeEnabled();
  });
});

describe("country and interface language", () => {
  it("renames the country in the new language and changes nothing else", async () => {
    const user = userEvent.setup();
    const patches = signedInWith("UZ");
    renderApp({ path: "/settings/profile" });

    await countryButton(/^country uzbekistan$/i);

    await openAccountMenu(user);
    await user.click(screen.getByRole("button", { name: "Русский" }));
    await user.keyboard("{Escape}");

    // Named in Russian now — and still the same country, unsaved, untouched.
    expect(await countryButton(/^страна узбекистан$/i)).toBeVisible();
    expect(saveButton()).toBeDisabled();
    expect(patches).toHaveLength(0);
  });

  it("does not change the interface language when a country is chosen", async () => {
    localStorage.setItem(LOCALE_STORAGE_KEY, "ru");
    const user = userEvent.setup();
    const patches = signedInWith("RU");
    renderApp({ path: "/settings/profile" });

    await user.click(await countryButton(/^страна россия$/i));
    // An English name, typed into a Russian interface, still finds it.
    await user.type(search(), "Uzbekistan{Enter}");
    await user.click(saveButton());

    await waitFor(() => expect(patches[0]?.country).toBe("UZ"));
    expect(document.documentElement.lang).toBe("ru");
    expect(localStorage.getItem(LOCALE_STORAGE_KEY)).toBe("ru");
    expect(await countryButton(/^страна узбекистан$/i)).toBeVisible();
  });
});

describe("the public profile", () => {
  it("shows the flag and the country's name in the reader's language", async () => {
    publicProfile("player_one", "UZ");
    const english = renderApp({ path: "/players/player_one" });

    const name = await screen.findByText("Uzbekistan");
    const item = name.closest("li");
    // "Country:" is for screen readers; the flag is for everyone else and
    // hidden from screen readers, which would read it as a second name.
    expect(item).toHaveTextContent(/^Country: 🇺🇿Uzbekistan$/);
    expect(within(item as HTMLElement).getByText("🇺🇿")).toHaveAttribute("aria-hidden", "true");
    english.unmount();

    localStorage.setItem(LOCALE_STORAGE_KEY, "ru");
    renderApp({ path: "/players/player_one" });

    expect(await screen.findByText("Узбекистан")).toBeVisible();
  });

  it("shows a code it cannot name as the letters that arrived, with no flag", async () => {
    publicProfile("legacy_player", "XX");
    renderApp({ path: "/players/legacy_player" });

    const item = (await screen.findByText(/^Country:/)).closest("li");
    expect(item).toHaveTextContent(/^Country: XX$/);
  });

  it("shows no country at all when the server sends none", async () => {
    publicProfile("private_player", null);
    renderApp({ path: "/players/private_player" });

    expect(await screen.findByRole("heading", { level: 1, name: "Player One" })).toBeVisible();
    expect(screen.queryByText(/^Country:/)).not.toBeInTheDocument();
  });
});
