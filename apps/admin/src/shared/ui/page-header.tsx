import type { ReactNode } from "react";

/**
 * A console page's heading and its one-line explanation — A64-024 hardening.
 *
 * Seven pages wrote the same `<h2>` plus muted paragraph by hand, and they
 * had drifted: some had a lede, some did not, and one carried its
 * description inside the first section instead. This is the shape they were
 * all approximating.
 *
 * `<h2>` rather than `<h1>` because the shell owns the document heading —
 * the page is a section of the console, and starting a second `<h1>` here
 * would give a screen reader two documents on one screen.
 *
 * `actions` sits beside the heading rather than below the lede, so a
 * primary control is reachable without reading past the description. It is
 * a slot rather than a prop shape: the pages that have one have very
 * different ones, and a `{label, onClick}` contract would fit none of them.
 */
export function PageHeader({
  title,
  description,
  actions,
}: {
  title: string;
  description?: string;
  actions?: ReactNode;
}) {
  return (
    <header className="page-header">
      <div>
        <h2>{title}</h2>
        {description !== undefined && <p className="muted">{description}</p>}
      </div>
      {actions !== undefined && <div className="page-header-actions">{actions}</div>}
    </header>
  );
}
