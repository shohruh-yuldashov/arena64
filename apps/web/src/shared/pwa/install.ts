import { useSyncExternalStore } from "react";

import { reportError } from "@/shared/lib/report-error";

/**
 * Installability, and the prompt the browser hands over — A64-020.9 §16.
 *
 * ## `beforeinstallprompt` is a one-shot loan
 *
 * Chromium fires it when it decides the site is installable, and the event
 * is only useful if `preventDefault()` is called synchronously. It fires
 * **early** — often before React has mounted — which is why
 * `watchInstallability()` runs from `main.tsx` at module scope rather than
 * from an effect, and why the event is captured into a module variable
 * rather than into component state that does not exist yet.
 *
 * The deferred event is held **in memory only** (§16). It is not
 * serialisable, and it belongs to the page that received it.
 *
 * ## What is persisted, and why exactly one thing is
 *
 * "Later" is remembered in `localStorage`. Nothing else is. §16 forbids
 * nagging, and a dismissal that lasted only until the next page load is a
 * nag with extra steps — the player would answer the same question every
 * session. It is a preference, not state: losing it costs one dismissable
 * bar, which is why storage being unavailable is not an error here.
 *
 * ## Timing is the widget's decision, not this module's
 *
 * This says whether an install *can* be offered. When it is offered —
 * after sign-in, never on first paint — is `widgets/pwa-notices`, because
 * that is the layer that knows there is a session. §16's "avoid
 * manipulative prompt timing" is a product rule, and it is enforced where
 * the product state lives.
 */

/**
 * Chromium's non-standard event. Declared here because no TypeScript DOM
 * lib ships it — it is a proposal, implemented by some browsers, absent in
 * others, and this file is the only place that has to know.
 */
interface BeforeInstallPromptEvent extends Event {
  prompt(): Promise<void>;
  readonly userChoice: Promise<{ outcome: "accepted" | "dismissed" }>;
}

export interface InstallState {
  /** A deferred prompt is held and can be shown. */
  readonly canPrompt: boolean;
  /** The browser told us it was installed during this session. */
  readonly installed: boolean;
  /** The player chose Later — persisted, see above. */
  readonly dismissed: boolean;
}

export const INSTALL_DISMISSED_KEY = "arena64.install-dismissed";

let deferred: BeforeInstallPromptEvent | null = null;
let state: InstallState = { canPrompt: false, installed: false, dismissed: readDismissed() };

const listeners = new Set<() => void>();

function setState(next: InstallState): void {
  if (
    next.canPrompt === state.canPrompt &&
    next.installed === state.installed &&
    next.dismissed === state.dismissed
  ) {
    return;
  }
  state = next;
  for (const listener of listeners) listener();
}

export function getInstallState(): InstallState {
  return state;
}

export function subscribeToInstall(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function useInstall(): InstallState {
  return useSyncExternalStore(subscribeToInstall, getInstallState, () => state);
}

/**
 * Starts listening. Called once from the browser entry point, before React
 * mounts, for the reason in this module's docstring.
 *
 * Returns a teardown so a test can install its own window and take it back
 * down; the application never calls it.
 */
export function watchInstallability(target: EventTarget = window): () => void {
  // Starting the watcher establishes the initial state from the platform:
  // nothing deferred yet, nothing installed yet, and whatever the stored
  // preference says. Written as a reset rather than as module
  // initialisation so that "when is the preference read" has one answer.
  deferred = null;
  setState({ canPrompt: false, installed: false, dismissed: readDismissed() });

  const controller = new AbortController();
  const { signal } = controller;

  target.addEventListener(
    "beforeinstallprompt",
    (event) => {
      // Without this the browser shows its own mini-infobar and the event
      // is spent. Arena64 asks at a moment that makes sense instead (§16).
      event.preventDefault();
      deferred = event as BeforeInstallPromptEvent;
      setState({ ...state, canPrompt: true, installed: false });
    },
    { signal },
  );

  target.addEventListener(
    "appinstalled",
    () => {
      // Installed — by our button or by the browser's own menu, which is
      // why this is a separate signal rather than something `promptInstall`
      // could infer from its own result.
      deferred = null;
      setState({ ...state, canPrompt: false, installed: true });
    },
    { signal },
  );

  return () => controller.abort();
}

export type InstallOutcome = "accepted" | "dismissed" | "unavailable";

/**
 * Shows the browser's install dialog.
 *
 * The deferred event is spent whether the player accepts or not — a second
 * `prompt()` on the same event throws — so it is released either way, and
 * a declined install simply leaves nothing to offer until the browser
 * decides to fire the event again.
 */
export async function promptInstall(): Promise<InstallOutcome> {
  const event = deferred;
  if (event === null) return "unavailable";
  deferred = null;

  try {
    await event.prompt();
    const { outcome } = await event.userChoice;
    setState({
      ...state,
      canPrompt: false,
      // `appinstalled` is the authority on installation and may not have
      // arrived yet, so acceptance is not recorded as installation here.
      installed: state.installed,
      // Accepting is not a dismissal; declining the browser's own dialog
      // is, and remembering it is what stops the bar coming back next
      // session for somebody who has already said no.
      dismissed: outcome === "dismissed" ? persistDismissed() : state.dismissed,
    });
    return outcome;
  } catch (error) {
    reportError(error, { scope: "pwa", stage: "install-prompt" });
    setState({ ...state, canPrompt: false });
    return "unavailable";
  }
}

/** "Later". Hides the bar and remembers the answer. */
export function dismissInstall(): void {
  setState({ ...state, dismissed: persistDismissed() });
}

function persistDismissed(): true {
  try {
    localStorage.setItem(INSTALL_DISMISSED_KEY, "1");
  } catch {
    /* Storage disabled or full. The bar hides for this session, which is
       the correct degradation for a preference — see the docstring. */
  }
  return true;
}

function readDismissed(): boolean {
  try {
    return localStorage.getItem(INSTALL_DISMISSED_KEY) === "1";
  } catch {
    return false;
  }
}
