/**
 * A failure the operator has to read — A64-024 hardening.
 *
 * `role="alert"` so it is announced when it appears: an error that renders
 * silently below the fold is one an operator does not know happened, and
 * every page here can fail while somebody is looking at a different part of
 * it.
 *
 * Thirteen copies of this element existed across twelve files. They agreed,
 * which is exactly why the duplication was worth removing before one of
 * them stopped agreeing — a `role` dropped in one place is invisible in
 * review and silent in use.
 *
 * Renders nothing for `null`, so a caller writes `<ErrorNotice message={x} />`
 * rather than guarding at every site.
 */
export function ErrorNotice({ message }: { message: string | null }) {
  if (message === null) return null;
  return (
    <p role="alert" className="error">
      {message}
    </p>
  );
}
