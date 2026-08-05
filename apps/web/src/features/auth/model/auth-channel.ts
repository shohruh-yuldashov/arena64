/**
 * Session events, across tabs — A64-020.2 §9.
 *
 * ## The failure this exists for
 *
 * The refresh cookie is shared by every tab on the origin; the access token
 * is not — each tab holds its own in memory. So signing out in one tab
 * revokes the session for all of them, and the others keep rendering a
 * signed-in UI with a token that is about to stop working. The user sees an
 * app that says they are logged in until something 401s.
 *
 * `BroadcastChannel` closes that gap: one tab announces, the rest clear.
 *
 * ## What is broadcast, and what is not
 *
 * **Never a token.** Not the access token, not the refresh token, not a
 * user id. A `BroadcastChannel` is readable by any script on the origin, so
 * anything put on it has the security properties of a global variable — and
 * a credential on a bus is a credential in `localStorage` with extra steps.
 *
 * What travels is the *fact*: this session ended. Each tab responds by
 * clearing its own memory and closing its own connections, which it can do
 * without being told anything sensitive.
 *
 * `session_refreshed` is deliberately **not** a message. A tab that
 * learned another tab had rotated the cookie would want the new access
 * token to go with it — and that is the one thing that cannot be sent. Each
 * tab refreshes for itself on its own `401`.
 */
export const AUTH_CHANNEL_NAME = "arena64-auth";

/** Every message this channel carries. Deliberately one. */
export type AuthBroadcast = { type: "logged_out" };

export interface AuthChannel {
  post: (message: AuthBroadcast) => void;
  subscribe: (handler: (message: AuthBroadcast) => void) => () => void;
  close: () => void;
}

/**
 * A channel, or a working no-op where `BroadcastChannel` is unavailable.
 *
 * The fallback is silence rather than a `localStorage`-event shim: the
 * degradation is that other tabs notice on their next failed request
 * instead of immediately, which is the behaviour every app had before this
 * API existed. A shim would mean writing session events to storage, which
 * is the one place this design is careful never to write.
 */
export function createAuthChannel(): AuthChannel {
  if (typeof BroadcastChannel === "undefined") {
    return { post: () => {}, subscribe: () => () => {}, close: () => {} };
  }

  const channel = new BroadcastChannel(AUTH_CHANNEL_NAME);

  return {
    post: (message) => {
      channel.postMessage(message);
    },
    subscribe(handler) {
      const listener = (event: MessageEvent<AuthBroadcast>) => {
        // Guarded: any script on the origin can post to a named channel,
        // so a message of an unexpected shape is ignored rather than
        // destructured.
        if (typeof event.data === "object" && event.data?.type === "logged_out") {
          handler(event.data);
        }
      };
      channel.addEventListener("message", listener);
      return () => {
        channel.removeEventListener("message", listener);
      };
    },
    close: () => {
      channel.close();
    },
  };
}
