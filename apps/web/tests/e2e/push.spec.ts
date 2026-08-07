import { expect, test } from "@playwright/test";

import { E2E_ACCOUNTS, resetPush, seededAccount, statePath } from "./accounts";

/**
 * Enabling and disabling push, in a real browser against the real API —
 * A64-021.6 §30.12.
 *
 * ## What this proves that the unit test cannot
 *
 * `src/features/notification-push/push.test.tsx` covers the same flow with
 * MSW, so the *client* logic is already asserted. What only this can show is
 * that the three endpoints exist, accept what this client actually sends,
 * and agree with each other — that a subscription registered by
 * `POST /push/subscriptions` is the one `GET /push/status` then counts, and
 * that removing it brings the count back down.
 *
 * It also runs against the **real service worker**: `navigator.serviceWorker`
 * is Chromium's, the worker that registers is the built one, and the
 * subscription is read back through a real `ServiceWorkerRegistration`.
 *
 * ## What is faked, and why — §28
 *
 * `Notification.permission` and `PushManager`. Both are limitations of
 * `chrome-headless-shell` rather than choices: it reports the permission as
 * `"denied"` whatever the context grants, and a real `subscribe()` rejects
 * with `AbortError` because no push service is configured.
 *
 * So what stays real here is the **network and the worker**: the app is the
 * built bundle, the service worker is the built one, the session is a real
 * cookie, and all three endpoints are the running API's. What is asserted
 * is what that combination can prove and the MSW test cannot — that the
 * endpoints exist, accept exactly what this client sends, and agree with
 * each other.
 *
 * It proves **nothing** about external Web Push delivery. Nothing in this
 * repository does, and nothing can without a real push service and a real
 * device. The encryption is proven by decrypting it as a user agent would
 * (`apps/api/tests/unit/test_web_push_protocol.py`), and the transport by a
 * stub service that answers every status code a real one can
 * (`test_push_notifications.py`).
 *
 * ## Cost
 *
 * **Zero registrations and zero extra accounts.** It borrows `alice`, whose
 * session the setup already holds.
 *
 * Sharing an account is safe *because* this project runs last (see
 * `playwright.config.ts`): the social suite has long finished with alice by
 * then, so there is no parallel contention to design around. A seventh
 * seeded account would have cost one more session probe in `global-setup`,
 * and the suite sits close enough to `refresh_ip`'s 30-per-minute that one
 * more was the difference between green and red.
 *
 * It writes a subscription row for alice and removes it again — and resets
 * before it starts, because the accounts accumulate state.
 */

/**
 * Stubs the two browser APIs `chrome-headless-shell` cannot provide.
 *
 * `Notification.permission` reports `"denied"` there whatever the context
 * grants — the Notification API is not backed in that binary — and
 * `PushManager.subscribe()` rejects with `AbortError` because there is no
 * push service configured, before any of this platform's code runs.
 *
 * Installed with `addInitScript`, so it is in place before a page script
 * runs and the application never sees the real ones.
 */
const P256DH = new Uint8Array(65).fill(4);
const AUTH = new Uint8Array(16).fill(7);
const ENDPOINT = "https://push.e2e.example.com/wpush/arena64-e2e";

test.use({
  storageState: statePath(E2E_ACCOUNTS.alice),
  permissions: ["notifications"],
});

test("a player turns push on for this browser, reloads, and turns it off", async ({
  page,
  request,
}) => {
  const reachable = await request
    .get("http://localhost:8000/health")
    .then((response) => response.ok())
    .catch(() => false);
  test.skip(!reachable, "apps/api is not running on :8000");

  // **The accounts accumulate state.** A run that enabled push and failed
  // before its last step leaves this account subscribed and the preference
  // on, so the next one would open the screen in `active` and never find the
  // button it came to press. Through the same endpoints a player uses.
  await resetPush(request, seededAccount(E2E_ACCOUNTS.alice), ENDPOINT);

  await page.addInitScript(
    ({ endpoint, p256dh, auth }) => {
      Object.defineProperty(Notification, "permission", {
        configurable: true,
        get: () => "granted",
      });
      Notification.requestPermission = () => Promise.resolve("granted");

      const keys: Record<string, ArrayBuffer> = {
        p256dh: new Uint8Array(p256dh).buffer,
        auth: new Uint8Array(auth).buffer,
      };
      // **Persisted across reloads**, because a real `PushManager` is: a
      // subscription belongs to the browser profile and survives a
      // navigation. `addInitScript` re-runs on every load, so a stub holding
      // its state in a closure would lose the subscription on reload — and
      // the reload assertion below would then be testing the stub rather
      // than the application.
      const SUBSCRIBED = "arena64-e2e-push-subscribed";
      const subscription = {
        endpoint,
        getKey: (name: string) => keys[name] ?? null,
        unsubscribe: () => {
          sessionStorage.removeItem(SUBSCRIBED);
          return Promise.resolve(true);
        },
      };
      Object.assign(PushManager.prototype, {
        getSubscription: () =>
          Promise.resolve(sessionStorage.getItem(SUBSCRIBED) ? subscription : null),
        subscribe: () => {
          sessionStorage.setItem(SUBSCRIBED, "1");
          return Promise.resolve(subscription);
        },
      });
    },
    { endpoint: ENDPOINT, p256dh: [...P256DH], auth: [...AUTH] },
  );

  await page.goto("/settings/notifications");

  // Push is off to begin with — the platform default, and a channel that
  // interrupts has to be asked for.
  const enable = page.getByRole("button", { name: /^enable$/i });
  await expect(enable).toBeVisible();

  await enable.click();

  // The **server's** answer, not the client's optimism: the section reaches
  // this state only after `POST /push/subscriptions` stored the row and the
  // preference was written.
  await expect(page.getByRole("button", { name: /turn off here/i })).toBeVisible();
  await expect(page.getByText(/receives tournament push notifications/i)).toBeVisible();

  // --- the reload: nothing about this state lives in the page ---
  //
  // §8. There is no `localStorage` key and no cached flag; the browser holds
  // the subscription and the backend holds the record, and a reload rebuilds
  // the answer from both. A client that had remembered it would pass this
  // and a client that had *only* remembered it would too — which is why the
  // assertion below is that the state survives a full navigation, where the
  // in-memory store does not.
  await page.reload();
  await expect(page.getByRole("button", { name: /turn off here/i })).toBeVisible();

  // --- off again, and the row is gone ---
  await page.getByRole("button", { name: /turn off here/i }).click();
  await expect(page.getByRole("button", { name: /^enable$/i })).toBeVisible();
});
