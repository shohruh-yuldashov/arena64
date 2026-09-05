import type { ReactNode } from "react";

/**
 * A titled band within a page — A64-027A §4.
 *
 * The console's pages are long, and before this task they were one column
 * of headings at the same weight as everything around them. A reader
 * scanning for "recent activity" had to read to find it.
 *
 * `<section>` with a real heading, so the structure a sighted reader gets
 * from the rule and the spacing is the structure a screen reader gets from
 * the landmark list. `aside` slots a control beside the title — a range
 * picker, a link to the full list — where it belongs with what it filters.
 */
export function Section({
  title,
  description,
  aside,
  children,
}: {
  title: string;
  description?: string;
  aside?: ReactNode;
  children: ReactNode;
}) {
  return (
    <section className="section">
      <div className="section__head">
        <div>
          <h3>{title}</h3>
          {description !== undefined && <p>{description}</p>}
        </div>
        {aside}
      </div>
      {children}
    </section>
  );
}

/** A label/value pair list, for a detail page's facts. */
export function KeyValueList({ children }: { children: ReactNode }) {
  return <dl className="facts">{children}</dl>;
}
