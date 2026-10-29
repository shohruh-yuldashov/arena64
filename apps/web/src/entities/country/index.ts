import { LOCALES } from "@/shared/i18n";

/**
 * A player's country — A64-031.F.
 *
 * ## The list is the backend's, copied once
 *
 * `ISO_3166_1_ALPHA_2` in `apps/api/app/modules/users/domain/validators.py`
 * is the authority: `PATCH /profile` accepts exactly those 249 codes and
 * nothing else. `Intl` can name a region but cannot enumerate them, so the
 * picker needs its own copy, and `country.test.ts` reads the Python set and
 * fails the moment the two differ. The direction of drift matters:
 *
 *     here, not there   the picker offers a country the save rejects
 *     there, not here   a stored code renders as its raw letters, no flag
 *
 * Grouped by first letter so a diff against the backend's sorted set reads
 * line by line.
 *
 * ## Names and flags are computed, not shipped
 *
 * `Intl.DisplayNames` names every code in all three locales the product
 * ships, so there is no translation table to keep and no country-data
 * dependency (CLAUDE.md §2.6). A flag is the code written in Unicode
 * regional-indicator letters, which every platform's emoji font draws —
 * except Windows, which shows the two letters instead. That is legible, and
 * the name beside it carries the meaning either way, so no flag images are
 * bundled for it.
 *
 * ## Country is not language
 *
 * Nothing here reads the interface language to *choose* a country, and
 * nothing infers one from the browser or the network. The locale decides
 * only how a country the player chose is **named**.
 */
const ASSIGNED_CODES = `
  AD AE AF AG AI AL AM AO AQ AR AS AT AU AW AX AZ
  BA BB BD BE BF BG BH BI BJ BL BM BN BO BQ BR BS BT BV BW BY BZ
  CA CC CD CF CG CH CI CK CL CM CN CO CR CU CV CW CX CY CZ
  DE DJ DK DM DO DZ
  EC EE EG EH ER ES ET
  FI FJ FK FM FO FR
  GA GB GD GE GF GG GH GI GL GM GN GP GQ GR GS GT GU GW GY
  HK HM HN HR HT HU
  ID IE IL IM IN IO IQ IR IS IT
  JE JM JO JP
  KE KG KH KI KM KN KP KR KW KY KZ
  LA LB LC LI LK LR LS LT LU LV LY
  MA MC MD ME MF MG MH MK ML MM MN MO MP MQ MR MS MT MU MV MW MX MY MZ
  NA NC NE NF NG NI NL NO NP NR NU NZ
  OM
  PA PE PF PG PH PK PL PM PN PR PS PT PW PY
  QA
  RE RO RS RU RW
  SA SB SC SD SE SG SH SI SJ SK SL SM SN SO SR SS ST SV SX SY SZ
  TC TD TF TG TH TJ TK TL TM TN TO TR TT TV TW TZ
  UA UG UM US UY UZ
  VA VC VE VG VI VN VU
  WF WS
  YE YT
  ZA ZM ZW
`;

export const COUNTRY_CODES: readonly string[] = ASSIGNED_CODES.trim().split(/\s+/);

const ASSIGNED = new Set(COUNTRY_CODES);

/**
 * The code a stored or typed value names, or `null` if it names none.
 *
 * `null`, empty and anything outside the list all come back `null`, so a
 * caller has one "no country" to handle. That includes a legacy value: the
 * column's check constraint is only `^[A-Z]{2}$`, and A64-012.1 stored
 * private-use codes such as `XX` before A64-012.3 started rejecting them.
 */
export function toCountryCode(value: string | null | undefined): string | null {
  if (value === null || value === undefined) return null;
  const code = value.trim().toUpperCase();
  return ASSIGNED.has(code) ? code : null;
}

/** 🇺🇿 for `UZ`: each letter shifted into the regional-indicator block. */
export function countryFlag(code: string): string {
  const REGIONAL_INDICATOR_A = 0x1f1e6;
  return String.fromCodePoint(
    ...[...code.toUpperCase()].map(
      (letter) => REGIONAL_INDICATOR_A + letter.charCodeAt(0) - 65,
    ),
  );
}

const displayNames = new Map<string, Intl.DisplayNames | null>();

/** The country's name in `locale` — or the code, where `Intl` cannot name it. */
export function countryName(code: string, locale: string): string {
  if (!displayNames.has(locale)) {
    let names: Intl.DisplayNames | null = null;
    try {
      names = new Intl.DisplayNames([locale], { type: "region" });
    } catch {
      /* An engine without `DisplayNames`. The code is still a true label. */
    }
    displayNames.set(locale, names);
  }
  return displayNames.get(locale)?.of(code) ?? code;
}

export interface Country {
  code: string;
  name: string;
  flag: string;
}

/** Every country, named in `locale` and sorted the way a reader of it sorts. */
export function countriesIn(locale: string): Country[] {
  const collator = new Intl.Collator(locale);
  return COUNTRY_CODES.map((code) => ({
    code,
    name: countryName(code, locale),
    flag: countryFlag(code),
  })).sort((a, b) => collator.compare(a.name, b.name));
}

/**
 * Folds a name for matching: no case, no accents, no punctuation.
 *
 * Punctuation goes because the three locales spell the same country with
 * different marks, and nobody types them: `Oʻzbekiston` is searched as
 * `ozbekiston`, `Кот-д’Ивуар` as `кот дивуар`, `Côte d’Ivoire` as
 * `cote divoire`. Decomposing first turns `ё` into `е` and `ô` into `o`.
 *
 * Modifier letters (`\p{Lm}`) go with the punctuation: Uzbek's `ʻ` is
 * U+02BB, which Unicode files as a letter, not a mark.
 */
function fold(text: string): string {
  return text
    .normalize("NFD")
    .replace(/\p{M}/gu, "")
    .toLowerCase()
    .replace(/[^\p{L}\p{N}]|\p{Lm}/gu, "");
}

/**
 * The countries a query finds, best first.
 *
 * A country matches on its name in **any** of the three locales, not only
 * the one on screen: a player reading Russian may well type `Uzbekistan`,
 * and the interface language says nothing about the alphabet somebody
 * reaches for. It also matches its own code exactly, so `GB` finds the
 * United Kingdom.
 *
 * Ranking is three tiers — the exact code, then names that start with the
 * query, then names that contain it — and within a tier the order is the
 * list's own, so results stay alphabetical in the reader's language.
 */
export function searchCountries(countries: readonly Country[], query: string): Country[] {
  const folded = fold(query);
  if (folded === "") return [...countries];

  const ranked: { country: Country; rank: number }[] = [];
  for (const country of countries) {
    const names = searchableNames(country.code);
    let rank: number | null = null;
    if (country.code.toLowerCase() === folded) rank = 0;
    else if (names.some((name) => name.startsWith(folded))) rank = 1;
    else if (names.some((name) => name.includes(folded))) rank = 2;
    if (rank !== null) ranked.push({ country, rank });
  }
  // `sort` is stable, so equal ranks keep the alphabetical order they came in.
  return ranked.sort((a, b) => a.rank - b.rank).map(({ country }) => country);
}

const searchable = new Map<string, string[]>();

function searchableNames(code: string): string[] {
  let names = searchable.get(code);
  if (names === undefined) {
    names = LOCALES.map((locale) => fold(countryName(code, locale)));
    searchable.set(code, names);
  }
  return names;
}
