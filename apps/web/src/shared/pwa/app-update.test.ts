import { expect, it, vi } from "vitest";

import {
  applyAppUpdate,
  getAppUpdateState,
  registerServiceWorker,
  type ServiceWorkerEnvironment,
} from "./app-update";
import { holdAppUpdate } from "./update-hold";

/**
 * The update lifecycle — A64-020.9 §28.2, §28.6, §28.7.
 *
 * Driven through the real `registerServiceWorker` with a fake
 * `ServiceWorkerContainer`, because the three properties worth asserting
 * are all about *when* this file acts, and none of them is observable from
 * outside the registration it wires.
 */

class FakeWorker extends EventTarget {
  state: ServiceWorker["state"] = "installed";
  readonly posted: unknown[] = [];
  postMessage(data: unknown): void {
    this.posted.push(data);
  }
}

class FakeRegistration extends EventTarget {
  waiting: FakeWorker | null = null;
  installing: FakeWorker | null = null;
  update = vi.fn(() => Promise.resolve());
}

class FakeContainer extends EventTarget {
  controller: unknown = null;
  registration = new FakeRegistration();
  register = vi.fn((_url: string, _options: unknown) => Promise.resolve(this.registration));
}

function environmentFor(
  container: FakeContainer | null,
  overrides: Partial<ServiceWorkerEnvironment> = {},
): ServiceWorkerEnvironment & { reload: ReturnType<typeof vi.fn> } {
  const reload = vi.fn();
  return {
    enabled: true,
    container: container as unknown as ServiceWorkerContainer | null,
    reload,
    ...overrides,
  } as ServiceWorkerEnvironment & { reload: ReturnType<typeof vi.fn> };
}

/** A registration that already has a replacement waiting for the user. */
function withWaitingWorker(): { container: FakeContainer; waiting: FakeWorker } {
  const container = new FakeContainer();
  const waiting = new FakeWorker();
  container.registration.waiting = waiting;
  // A controller means a *previous* worker is running this page — the
  // difference between an update and a first install.
  container.controller = {};
  return { container, waiting };
}

it("registers one worker at the root scope, and only in a production build", async () => {
  // `npm run dev` — §8. A stale worker between a developer and Vite's HMR
  // is the failure this refusal exists to prevent.
  const development = new FakeContainer();
  expect(
    await registerServiceWorker(environmentFor(development, { enabled: false })),
  ).toBeNull();
  expect(development.register).not.toHaveBeenCalled();

  // A browser without service workers, or a page served over plain HTTP.
  expect(await registerServiceWorker(environmentFor(null))).toBeNull();

  const production = new FakeContainer();
  await registerServiceWorker(environmentFor(production));

  expect(production.register).toHaveBeenCalledTimes(1);
  expect(production.register).toHaveBeenCalledWith("/sw.js", {
    scope: "/",
    updateViaCache: "none",
  });
});

it("announces a waiting worker and activates it only when asked", async () => {
  const { container, waiting } = withWaitingWorker();
  const environment = environmentFor(container);

  await registerServiceWorker(environment);

  expect(getAppUpdateState()).toEqual({ status: "available", dismissed: false });
  // §14: noticing an update must not reload anything.
  expect(environment.reload).not.toHaveBeenCalled();

  expect(applyAppUpdate()).toBe(true);
  // The worker is asked to step aside. It is the worker that decides when,
  // and this page still has not reloaded.
  expect(waiting.posted).toEqual([{ type: "arena64/skip-waiting" }]);
  expect(getAppUpdateState().status).toBe("activating");
  expect(environment.reload).not.toHaveBeenCalled();

  // Only once the new worker has actually taken over.
  container.dispatchEvent(new Event("controllerchange"));
  expect(environment.reload).toHaveBeenCalledTimes(1);
});

it("refuses to activate while a live surface holds the update", async () => {
  const { container, waiting } = withWaitingWorker();
  const environment = environmentFor(container);
  await registerServiceWorker(environment);

  // What `/games/$matchId` does for as long as a game is running.
  const release = holdAppUpdate();

  expect(applyAppUpdate()).toBe(false);
  expect(waiting.posted).toEqual([]);
  // The prompt stays visible and stays honest: still available, not
  // dismissed, and above all not activating (§14).
  expect(getAppUpdateState()).toEqual({ status: "available", dismissed: false });

  release();

  expect(applyAppUpdate()).toBe(true);
  expect(waiting.posted).toEqual([{ type: "arena64/skip-waiting" }]);

  // Let the activation finish, so no timer outlives the test.
  container.dispatchEvent(new Event("controllerchange"));
});
