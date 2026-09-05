import { type ReactNode, useId } from "react";

import { useTranslation } from "@/shared/i18n";
import { Icon } from "@/shared/ui/icon";

/**
 * One toolbar for every management page — A64-027A.3 §7.
 *
 * Before this task each page assembled its own: a `<p class="field">` per
 * control, a search input with its helper sentence sitting *beside* it as a
 * loose paragraph, and no way to tell at a glance whether a filter was
 * applied. Five pages, five arrangements, and the helper text was the thing
 * that broke the composition on every one of them.
 *
 * ## Helper text moves under the field
 *
 * §7 permits it. A sentence beside a search box competes with the box for
 * the eye and pushes the filters into a second row; under it, it reads as
 * what it is — an explanation nobody needs after the first visit.
 *
 * ## An applied filter says so
 *
 * The count of active filters is rendered beside a clear control, and
 * neither appears when nothing is filtered. An operator who cannot see that
 * a filter is on reads an empty table as "there is nothing here", which is
 * the single most expensive misreading an operations console can cause.
 */
export function FilterToolbar({
  search,
  filters,
  activeCount,
  onClear,
  actions,
}: {
  /** The search control, if the page has one. */
  search?: ReactNode;
  /** The select controls, laid out in one wrapping row. */
  filters?: ReactNode;
  /** How many filters are applied right now. Zero hides the clear control. */
  activeCount?: number;
  onClear?: () => void;
  /** A page-level action, right-aligned. The primary button lives here. */
  actions?: ReactNode;
}) {
  const { t } = useTranslation();
  const applied = activeCount ?? 0;

  return (
    <div className="toolbar-block">
      <div className="toolbar-block__row">
        {search}
        {filters !== undefined && <div className="toolbar-block__filters">{filters}</div>}

        {applied > 0 && onClear !== undefined && (
          <button
            type="button"
            className="action subtle toolbar-block__clear"
            onClick={onClear}
          >
            <Icon name="close" size={15} />
            {t("filters.clear", { count: String(applied) })}
          </button>
        )}

        {actions !== undefined && <div className="toolbar-block__actions">{actions}</div>}
      </div>
    </div>
  );
}

/**
 * A labelled search box.
 *
 * The label is visible rather than `sr-only`: A64-027A.2 hid it behind the
 * placeholder, and a placeholder is not a label — it disappears the moment
 * somebody types, which is exactly when they might look for it.
 */
export function SearchField({
  value,
  onChange,
  label,
  hint,
  placeholder,
}: {
  value: string;
  onChange: (next: string) => void;
  label: string;
  hint?: string;
  placeholder?: string;
}) {
  const id = useId();
  const hintId = `${id}-hint`;

  return (
    <div className="field toolbar-block__search">
      <label htmlFor={id} className="field__label">
        {label}
      </label>
      <span className="search">
        <Icon name="search" size={16} />
        <input
          id={id}
          type="search"
          value={value}
          placeholder={placeholder}
          aria-describedby={hint === undefined ? undefined : hintId}
          onChange={(event) => {
            onChange(event.target.value);
          }}
        />
      </span>
      {hint !== undefined && (
        <span id={hintId} className="field__hint">
          {hint}
        </span>
      )}
    </div>
  );
}

/** A labelled select, sized to its content rather than to the row. */
export function SelectField({
  value,
  onChange,
  label,
  children,
}: {
  value: string;
  onChange: (next: string) => void;
  label: string;
  children: ReactNode;
}) {
  const id = useId();
  return (
    <div className="field toolbar-block__select">
      <label htmlFor={id} className="field__label">
        {label}
      </label>
      <select
        id={id}
        value={value}
        onChange={(event) => {
          onChange(event.target.value);
        }}
      >
        {children}
      </select>
    </div>
  );
}
