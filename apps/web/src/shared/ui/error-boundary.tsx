import { Component, type ErrorInfo, type ReactNode } from "react";

import { reportError } from "@/shared/lib/report-error";

interface ErrorBoundaryProps {
  children: ReactNode;
  /** Rendered instead of `children` once a descendant has thrown. */
  fallback: (state: { error: Error; reset: () => void }) => ReactNode;
  /** Additional context for the report — which boundary caught it. */
  scope?: string;
}

interface ErrorBoundaryState {
  error: Error | null;
}

/**
 * The only class component in this codebase, and it has to be.
 *
 * React exposes error catching through `componentDidCatch` /
 * `getDerivedStateFromError` and has no hook equivalent — a function
 * component cannot catch a render-time throw from a child. So this is a
 * class, deliberately, and everything above it is a function.
 *
 * ## What it catches, and what it does not
 *
 * Catches: anything thrown during render, in a lifecycle method, or in a
 * constructor, anywhere below it.
 *
 * Does **not** catch: event handlers, `setTimeout`, or rejected promises —
 * React never sees those, so nothing can. That is why `shared/api` throws
 * typed errors a caller handles, rather than relying on a boundary to be
 * the safety net for asynchronous failure.
 *
 * ## `reset`, and why the fallback receives it
 *
 * Without a way back, an error state is permanent until a full reload, and
 * a user who hit a transient failure has to lose their place to recover
 * from it. `reset` clears the caught error and re-renders the subtree.
 *
 * The error is **always reported** before the fallback renders — a
 * boundary that silently showed a friendly page would be CLAUDE.md §2.7's
 * silent failure with a nicer face on it.
 */
export class ErrorBoundary extends Component<ErrorBoundaryProps, ErrorBoundaryState> {
  override state: ErrorBoundaryState = { error: null };

  static getDerivedStateFromError(error: Error): ErrorBoundaryState {
    return { error };
  }

  override componentDidCatch(error: Error, info: ErrorInfo): void {
    reportError(error, {
      scope: this.props.scope ?? "error-boundary",
      componentStack: info.componentStack,
    });
  }

  private readonly reset = (): void => {
    this.setState({ error: null });
  };

  override render(): ReactNode {
    const { error } = this.state;
    if (error !== null) {
      return this.props.fallback({ error, reset: this.reset });
    }
    return this.props.children;
  }
}
