import { useSyncExternalStore } from "react";

import { reportError } from "@/shared/lib/report-error";

import { SKIP_WAITING_MESSAGE } from "../../../pwa/cache-policy";
import { isAppUpdateHeld } from "./update-hold";

/**
 * Registering the service worker, and the update lifecycle it produces —
 * A64-020.9 §8, §14.
 *
 * ## The lifecycle, and who decides each step
 *
 *     browser    downloads and installs the new worker in the background
 *     worker     does *not* skip waiting — `pwa/service-worker.ts`
 *     this file  notices `waiting` and publishes `status: "available"`
 *     the user   presses Update
 *     this file  posts the one message the worker understands
 *     worker     skips waiting, activates, claims the page
 *     this file  reloads — **once**, and only because Update was pressed
 *
 * Nothing in that sequence reloads a page on its own. `controllerchange`
 * fires on a first install too (the worker claims the page), and reloading
 * there would refresh every visitor's first page load for no reason — so
 * the reload is gated on `activationRequested`, which only `applyAppUpdate`
 * sets.
 *
 * ## Why a module-level store
 *
 * There is one service worker per origin and one registration per page, so
 * the state is genuinely global: a React context would let two subtrees
 * disagree about whether an update is waiting. `useSyncExternalStore` is
 * how React reads it, and the state surviving navigation is a property of
 * the module rather than of any component that happens to stay mounted —
 * §15's "state survives route navigation during the same session".
 */

export type AppUpdateStatus =
  /** Nothing waiting. */
  | "idle"
  /** A new worker is installed and waiting for the user's word. */
  | "available"
  /** The user said yes; the worker is taking over. */
  | "activating";

export interface AppUpdateState {
  readonly status: AppUpdateStatus;
  /** The user chose Later. Reset only by a *different* waiting worker. */
  readonly dismissed: boolean;
}

/**
 * What `registerServiceWorker` needs from the platform.
 *
 * Injected rather than read from globals so both halves of §28's second
 * test are reachable: that a development build registers nothing, and that
 * a production build registers exactly one worker at the root scope. A
 * test that had to fake `navigator` and `import.meta.env` would be
 * asserting against its own mocks.
 */
export interface ServiceWorkerEnvironment {
  /** Production builds only — §8. `npm run dev` must have no worker. */
  readonly enabled: boolean;
  /**
   * `null` when the browser has no service workers, or when the page is
   * not a secure context — the same check, because `navigator.serviceWorker`
   * is absent over plain HTTP everywhere except `localhost`.
   */
  readonly container: ServiceWorkerContainer | null;
  /** How the page reloads once the new worker has taken over. */
  readonly reload: () => void;
}

export const SERVICE_WORKER_URL = "/sw.js";
/** The scope the manifest claims. No broader — §31. */
export const SERVICE_WORKER_SCOPE = "/";

/**
 * How long to wait for the new worker to take over before offering the
 * button again — §26's "update activation failure".
 *
 * A worker can fail to activate: an unhandled rejection in its `activate`
 * handler, storage that has gone away underneath it. Without this the UI
 * would sit on "updating…" for the rest of the session with no way back.
 */
const ACTIVATION_TIMEOUT_MS = 10_000;

const IDLE: AppUpdateState = { status: "idle", dismissed: false };

let state: AppUpdateState = IDLE;
let registration: ServiceWorkerRegistration | null = null;
/** The waiting worker already announced, so it is announced once. */
let announced: ServiceWorker | null = null;
let activationRequested = false;
let activationTimer: ReturnType<typeof setTimeout> | null = null;
/** Cancels the listeners of a previous registration. */
let lifecycle: AbortController | null = null;

const listeners = new Set<() => void>();

function setState(next: AppUpdateState): void {
  if (next.status === state.status && next.dismissed === state.dismissed) return;
  state = next;
  for (const listener of listeners) listener();
}

export function getAppUpdateState(): AppUpdateState {
  return state;
}

export function subscribeToAppUpdate(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function useAppUpdate(): AppUpdateState {
  return useSyncExternalStore(subscribeToAppUpdate, getAppUpdateState, () => IDLE);
}

export function defaultServiceWorkerEnvironment(): ServiceWorkerEnvironment {
  const supported = typeof navigator !== "undefined" && "serviceWorker" in navigator;
  return {
    enabled: import.meta.env.PROD,
    container: supported ? navigator.serviceWorker : null,
    reload: () => window.location.reload(),
  };
}

/**
 * Registers the worker and wires the update lifecycle. Idempotent by
 * construction: a second call abandons the first registration's listeners
 * and starts from `idle`.
 *
 * **Never throws.** A browser in private mode, an origin without HTTPS, a
 * storage quota already spent — every one of those must leave a working
 * web application rather than a blank page (§26), so a failure here is
 * reported and swallowed deliberately, which is the one place CLAUDE.md
 * §9.2 allows it: there is no caller that could do anything about it.
 */
export async function registerServiceWorker(
  environment: ServiceWorkerEnvironment = defaultServiceWorkerEnvironment(),
): Promise<ServiceWorkerRegistration | null> {
  lifecycle?.abort();
  lifecycle = new AbortController();
  const { signal } = lifecycle;

  registration = null;
  announced = null;
  activationRequested = false;
  setState(IDLE);

  if (!environment.enabled || environment.container === null) return null;

  const container = environment.container;

  try {
    const registered = await container.register(SERVICE_WORKER_URL, {
      scope: SERVICE_WORKER_SCOPE,
      // The worker script itself is never served from the HTTP cache. A
      // worker cached for an hour is an update nobody can see for an hour,
      // and this is the one file where that matters.
      updateViaCache: "none",
    });
    registration = registered;

    // Already waiting when we arrived — a tab opened after another one
    // downloaded the update. `controller !== null` distinguishes that from
    // a first install, where nothing is being replaced.
    if (registered.waiting !== null && container.controller !== null) {
      announce(registered.waiting);
    }

    registered.addEventListener(
      "updatefound",
      () => {
        const installing = registered.installing;
        if (installing === null) return;
        installing.addEventListener(
          "statechange",
          () => {
            if (installing.state !== "installed") return;
            // No controller means this is the first install on this
            // device. Nothing is being replaced, so there is nothing to
            // announce and nothing for the user to decide.
            if (container.controller === null) return;
            announce(installing);
          },
          { signal },
        );
      },
      { signal },
    );

    container.addEventListener(
      "controllerchange",
      () => {
        if (!activationRequested) return;
        clearActivationTimer();
        environment.reload();
      },
      { signal },
    );

    // The browser checks for a new worker on navigation, and this
    // application does not navigate — the router swaps components inside
    // one document. So returning to the tab is the moment worth checking,
    // and it is the only one this file schedules: a polling interval would
    // spend a mobile connection on a question whose answer changes at
    // deploy frequency.
    document.addEventListener(
      "visibilitychange",
      () => {
        if (document.hidden) return;
        void registered.update().catch(() => {
          // Offline, or the server did not answer. Expected rather than
          // exceptional: the next check will ask again, and reporting it
          // would bury real failures under every subway ride.
        });
      },
      { signal },
    );

    return registered;
  } catch (error) {
    reportError(error, { scope: "pwa", stage: "register" });
    return null;
  }
}

function announce(worker: ServiceWorker): void {
  if (announced === worker) return;
  announced = worker;
  // A genuinely new worker clears a previous dismissal: "Later" was an
  // answer about *that* version, not a standing refusal to ever update.
  setState({ status: "available", dismissed: false });
}

/**
 * Activates the waiting worker, at the user's request.
 *
 * Returns `false` and does nothing when an update is held (§14) or when
 * nothing is waiting. The prompt already hides the button in the first
 * case; this is the second line, so that a caller which forgets cannot
 * reload a page mid-game.
 */
export function applyAppUpdate(): boolean {
  if (isAppUpdateHeld()) return false;

  const waiting = registration?.waiting ?? null;
  if (waiting === null) return false;

  activationRequested = true;
  setState({ status: "activating", dismissed: false });
  waiting.postMessage({ type: SKIP_WAITING_MESSAGE });

  clearActivationTimer();
  activationTimer = setTimeout(() => {
    // The worker never took over. Offer the button again rather than
    // leaving a spinner that outlives the session.
    activationRequested = false;
    setState({ status: "available", dismissed: false });
  }, ACTIVATION_TIMEOUT_MS);

  return true;
}

export function dismissAppUpdate(): void {
  if (state.status !== "available") return;
  setState({ status: "available", dismissed: true });
}

function clearActivationTimer(): void {
  if (activationTimer === null) return;
  clearTimeout(activationTimer);
  activationTimer = null;
}
