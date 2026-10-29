import { CheckIcon, ChevronsUpDownIcon, SearchIcon, XIcon } from "lucide-react";
import { type KeyboardEvent, useEffect, useId, useMemo, useRef, useState } from "react";

import {
  countriesIn,
  countryFlag,
  countryName,
  searchCountries,
  toCountryCode,
} from "@/entities/country";
import { useTranslation } from "@/shared/i18n";
import { cn } from "@/shared/lib/cn";
import { Dialog, DialogContent, DialogTitle, DialogTrigger } from "@/shared/ui";

/**
 * The country picker on the profile form — A64-031.F.
 *
 * A button that shows the choice, and a dialog with a search box over the
 * list. `""` is "no country", the form's spelling of the API's `null`.
 *
 * ## Why a dialog, and not a dropdown under the field
 *
 * 249 entries need a search box, and a search box on a phone raises the
 * keyboard, which covers the lower half of the screen — exactly where a
 * dropdown under a field near the bottom of a form would be. The dialog is
 * pinned to the **top** of a small viewport for that reason, and centred
 * from `sm` up where there is room. It is also the one overlay primitive the
 * repository has: Radix supplies the focus trap, `Escape`, and the return of
 * focus to the trigger, none of which is re-implemented here.
 *
 * ## Accessibility
 *
 *     trigger   a button named by the label and the current value together
 *               — "Country, Uzbekistan" — with `aria-haspopup="dialog"`
 *     search    the WAI-ARIA combobox: focus stays in the input, the arrow
 *               keys move `aria-activedescendant` through the listbox, Enter
 *               chooses, Escape closes
 *     flags     `aria-hidden` — "flag: Uzbekistan, Uzbekistan" is noise,
 *               and on Windows the glyph is two letters anyway
 *
 * The highlighted option is the one marked `aria-selected`, as the pattern
 * specifies; the saved value is shown by a check mark and announced by the
 * trigger, which is what a screen-reader user hears on returning to it.
 */
export function CountrySelect({
  label,
  value,
  onChange,
  onBlur,
  description,
  error,
}: {
  label: string;
  /** An ISO 3166-1 alpha-2 code, `""` for none, or a stored value no longer on the list. */
  value: string;
  onChange: (value: string) => void;
  onBlur?: () => void;
  description?: string | undefined;
  error?: string | undefined;
}) {
  const { t, locale } = useTranslation();
  const [open, setOpen] = useState(false);
  const id = useId();
  const labelId = `${id}-label`;
  const valueId = `${id}-value`;
  const descriptionId = `${id}-description`;
  const errorId = `${id}-error`;

  const code = toCountryCode(value);
  const describedBy =
    [error !== undefined ? errorId : null, description !== undefined ? descriptionId : null]
      .filter(Boolean)
      .join(" ") || undefined;

  const choose = (next: string) => {
    setOpen(false);
    if (next !== value) onChange(next);
  };

  return (
    <div className="flex flex-col gap-1.5">
      <span id={labelId} className="text-sm font-medium">
        {label}
      </span>
      <Dialog
        open={open}
        onOpenChange={(next) => {
          setOpen(next);
          if (!next) onBlur?.();
        }}
      >
        <DialogTrigger asChild>
          <button
            type="button"
            aria-labelledby={`${labelId} ${valueId}`}
            aria-invalid={error !== undefined}
            {...(describedBy !== undefined ? { "aria-describedby": describedBy } : {})}
            className={cn(
              "border-input dark:bg-input/30 flex h-11 w-full min-w-0 items-center gap-2.5 rounded-md border bg-transparent px-3 text-left text-base shadow-xs transition-[color,box-shadow] outline-none md:text-sm",
              "focus-visible:border-ring focus-visible:ring-ring/50 focus-visible:ring-[3px]",
              "aria-invalid:ring-destructive/20 dark:aria-invalid:ring-destructive/40 aria-invalid:border-destructive",
            )}
          >
            {/* A fixed box, filled or not, so choosing a country does not
                shift the name sideways. */}
            <span aria-hidden="true" className="w-6 shrink-0 text-center text-lg leading-none">
              {code !== null ? countryFlag(code) : null}
            </span>
            <span
              id={valueId}
              className={cn("min-w-0 flex-1 truncate", value === "" && "text-muted-foreground")}
            >
              {code !== null
                ? countryName(code, locale)
                : value === ""
                  ? t("profile.edit.countryNone")
                  : value}
            </span>
            <ChevronsUpDownIcon
              aria-hidden="true"
              className="text-muted-foreground size-4 shrink-0"
            />
          </button>
        </DialogTrigger>

        <DialogContent
          aria-describedby={undefined}
          className="top-4 flex max-h-[calc(100dvh-2rem)] translate-y-0 flex-col gap-3 p-4 sm:top-1/2 sm:-translate-y-1/2 sm:p-6"
        >
          <DialogTitle>{label}</DialogTitle>
          {open && <CountryList value={value} onChoose={choose} />}
        </DialogContent>
      </Dialog>

      {description !== undefined && (
        <p id={descriptionId} className="text-muted-foreground text-xs">
          {description}
        </p>
      )}
      {error !== undefined && (
        <p id={errorId} role="alert" className="text-destructive text-sm font-medium">
          {error}
        </p>
      )}
    </div>
  );
}

/** The search box and the list. Mounted per opening, so each starts clean. */
function CountryList({
  value,
  onChoose,
}: {
  value: string;
  onChoose: (value: string) => void;
}) {
  const { t, locale } = useTranslation();
  const id = useId();
  const listId = `${id}-list`;
  const optionId = (index: number) => `${id}-option-${index}`;

  const countries = useMemo(() => countriesIn(locale), [locale]);
  const [query, setQuery] = useState("");

  // "Remove country" heads the list while nothing is typed and something is
  // chosen: it is an option like the others, reachable the same way.
  const options = useMemo(() => {
    const found = searchCountries(countries, query).map((country) => ({
      value: country.code,
      flag: country.flag,
      name: country.name,
    }));
    return query.trim() === "" && value !== ""
      ? [{ value: "", flag: null, name: t("profile.edit.countryRemove") }, ...found]
      : found;
  }, [countries, query, value, t]);

  // Opens on the saved country, so a player checking what they chose sees
  // it highlighted rather than the first entry of the alphabet.
  const [active, setActive] = useState(() =>
    Math.max(
      0,
      options.findIndex((option) => option.value === value && value !== ""),
    ),
  );
  const listRef = useRef<HTMLUListElement>(null);
  const openedOn = useRef(active);

  useEffect(() => {
    reveal(document.getElementById(`${id}-option-${openedOn.current}`));
  }, [id]);

  const highlight = (index: number) => {
    setActive(index);
    reveal(document.getElementById(optionId(index)));
  };

  const onKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    const last = options.length - 1;
    if (event.key === "ArrowDown") {
      event.preventDefault();
      if (last >= 0) highlight(Math.min(active + 1, last));
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      if (last >= 0) highlight(Math.max(active - 1, 0));
    } else if (event.key === "Enter") {
      // Never a form submit from inside the picker, whether or not
      // anything matched.
      event.preventDefault();
      const option = options[active];
      if (option !== undefined) onChoose(option.value);
    }
  };

  const activeOption = options[active];

  return (
    <>
      <div className="relative">
        <SearchIcon
          aria-hidden="true"
          className="text-muted-foreground pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2"
        />
        <input
          type="text"
          role="combobox"
          aria-label={t("profile.edit.countrySearch")}
          aria-expanded="true"
          aria-controls={listId}
          aria-autocomplete="list"
          {...(activeOption !== undefined ? { "aria-activedescendant": optionId(active) } : {})}
          placeholder={t("profile.edit.countrySearch")}
          autoComplete="off"
          spellCheck={false}
          value={query}
          onChange={(event) => {
            setQuery(event.target.value);
            setActive(0);
            if (listRef.current !== null) listRef.current.scrollTop = 0;
          }}
          onKeyDown={onKeyDown}
          className="border-input placeholder:text-muted-foreground dark:bg-input/30 focus-visible:border-ring focus-visible:ring-ring/50 flex h-11 w-full min-w-0 rounded-md border bg-transparent pr-3 pl-9 text-base shadow-xs outline-none focus-visible:ring-[3px] md:text-sm"
        />
      </div>

      {/* A fixed height whatever the result count, so the dialog does not
          jump about while somebody types. */}
      <ul
        ref={listRef}
        id={listId}
        role="listbox"
        aria-label={t("profile.edit.country")}
        className="h-[min(24rem,55dvh)] overflow-y-auto overscroll-contain rounded-md border p-1"
      >
        {options.map((option, index) => {
          const isActive = index === active;
          const isChosen = option.value === value;
          return (
            <li
              key={option.value === "" ? "none" : option.value}
              id={optionId(index)}
              role="option"
              aria-selected={isActive}
              onPointerMove={() => {
                if (!isActive) setActive(index);
              }}
              // `mousedown` would steal focus from the search box first; the
              // click is what chooses.
              onMouseDown={(event) => event.preventDefault()}
              onClick={() => onChoose(option.value)}
              className={cn(
                "flex min-h-11 cursor-pointer items-center gap-3 rounded-sm px-2 text-sm select-none",
                isActive && "bg-accent text-accent-foreground",
              )}
            >
              <span
                aria-hidden="true"
                className="w-6 shrink-0 text-center text-lg leading-none"
              >
                {option.flag ?? <XIcon className="text-muted-foreground mx-auto size-4" />}
              </span>
              <span className="min-w-0 flex-1 break-words">{option.name}</span>
              {isChosen && option.value !== "" && (
                <CheckIcon aria-hidden="true" className="text-primary size-4 shrink-0" />
              )}
            </li>
          );
        })}
      </ul>

      <p aria-live="polite" className="text-muted-foreground min-h-5 text-sm">
        {options.length === 0
          ? t("profile.edit.countryNoMatch", { query: query.trim() })
          : null}
      </p>
    </>
  );
}

/**
 * Scrolls an option into the list's view, `nearest` so that moving between
 * rows already visible never scrolls at all. Guarded because jsdom has no
 * layout and does not implement it.
 */
function reveal(option: HTMLElement | null) {
  if (typeof option?.scrollIntoView === "function") option.scrollIntoView({ block: "nearest" });
}
