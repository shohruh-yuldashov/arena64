import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, it } from "vitest";

import {
  countriesIn,
  COUNTRY_CODES,
  countryFlag,
  countryName,
  searchCountries,
  toCountryCode,
} from "@/entities/country";

/**
 * The country entity — A64-031.F.
 *
 * Pure functions over `Intl` and a constant, so these run without the app.
 * What the picker and the profile header do with them is asserted through
 * the real app in `features/profile/country.test.tsx`.
 */
const VALIDATORS = resolve(process.cwd(), "../api/app/modules/users/domain/validators.py");

/** The codes in the backend's `ISO_3166_1_ALPHA_2` frozenset, read from its source. */
function backendCodes(): string[] {
  const source = readFileSync(VALIDATORS, "utf8");
  const body = /ISO_3166_1_ALPHA_2: frozenset\[str\] = frozenset\(\s*\[([\s\S]*?)\]\s*\)/.exec(
    source,
  )?.[1];
  if (body === undefined) throw new Error("ISO_3166_1_ALPHA_2 not found in validators.py");
  return [...body.matchAll(/"([A-Z]{2})"/g)].map((match) => match[1] ?? "");
}

describe("the country list", () => {
  it("is exactly the set the profile API accepts", () => {
    // The backend is the authority — a code offered here and missing there
    // is a choice the save rejects with a 422.
    const backend = backendCodes();

    expect(backend.length).toBeGreaterThan(200);
    expect([...COUNTRY_CODES].sort()).toEqual([...backend].sort());
    expect(new Set(COUNTRY_CODES).size).toBe(COUNTRY_CODES.length);
  });

  it("names every country in all three languages, sorted for each", () => {
    for (const locale of ["uz", "ru", "en"]) {
      const countries = countriesIn(locale);
      const collator = new Intl.Collator(locale);

      expect(countries).toHaveLength(COUNTRY_CODES.length);
      // A name, not the code echoed back as the fallback.
      expect(countries.filter((country) => country.name === country.code)).toEqual([]);
      for (let index = 1; index < countries.length; index += 1) {
        const previous = countries[index - 1]?.name ?? "";
        expect(collator.compare(previous, countries[index]?.name ?? "")).toBeLessThanOrEqual(0);
      }
    }
  });
});

describe("a stored or submitted value", () => {
  it.each([
    ["UZ", "UZ"],
    ["uz", "UZ"],
    [" gb ", "GB"],
  ])("%j names %s", (value, code) => {
    expect(toCountryCode(value)).toBe(code);
  });

  it.each([null, undefined, "", "  ", "XX", "ZZ", "QQ", "GBR", "U", "1A", "Uzbekistan"])(
    "%j names no country",
    (value) => {
      // Including the private-use codes A64-012.1 accepted before
      // A64-012.3 started rejecting them — they may still be stored.
      expect(toCountryCode(value)).toBeNull();
    },
  );
});

describe("flags and names", () => {
  it("writes the flag as the code's two regional-indicator letters", () => {
    expect(countryFlag("UZ")).toBe("🇺🇿");
    expect(countryFlag("GB")).toBe("🇬🇧");
    expect([...countryFlag("RU")]).toHaveLength(2);
  });

  it("names a country in the reader's language", () => {
    expect(countryName("UZ", "en")).toBe("Uzbekistan");
    expect(countryName("UZ", "ru")).toBe("Узбекистан");
    expect(countryName("UZ", "uz")).toBe("Oʻzbekiston");
  });
});

describe("searching", () => {
  const english = countriesIn("en");
  const russian = countriesIn("ru");
  const codes = (found: { code: string }[]) => found.map((country) => country.code);

  it("returns every country, in order, for an empty query", () => {
    expect(searchCountries(english, "  ")).toEqual(english);
  });

  it("finds by any part of the name, ignoring case", () => {
    expect(codes(searchCountries(english, "uzbek"))).toEqual(["UZ"]);
    expect(codes(searchCountries(english, "KINGDOM"))).toEqual(["GB"]);
  });

  it("puts names that start with the query before names that only contain it", () => {
    // "Guinea" starts with it; "Equatorial Guinea" and "Papua New Guinea"
    // only contain it, and stay alphabetical behind it.
    const found = codes(searchCountries(english, "guinea"));

    expect(found[0]).toBe("GN");
    expect(found.indexOf("GN")).toBeLessThan(found.indexOf("GQ"));
    expect(found.indexOf("GQ")).toBeLessThan(found.indexOf("PG"));
  });

  it("finds a country by its exact code first", () => {
    expect(codes(searchCountries(english, "gb"))[0]).toBe("GB");
  });

  it("ignores accents, apostrophes and hyphens nobody types", () => {
    expect(codes(searchCountries(countriesIn("uz"), "ozbekiston"))).toEqual(["UZ"]);
    expect(codes(searchCountries(english, "cote divoire"))).toEqual(["CI"]);
    expect(codes(searchCountries(russian, "кот дивуар"))).toEqual(["CI"]);
  });

  it("finds a country by its name in another of the three languages", () => {
    // A Russian interface, and the player types the English name.
    expect(codes(searchCountries(russian, "Uzbekistan"))).toEqual(["UZ"]);
    expect(codes(searchCountries(english, "Узбекистан"))).toEqual(["UZ"]);
  });

  it("finds nothing for a query no country matches", () => {
    expect(searchCountries(english, "atlantis")).toEqual([]);
  });
});
