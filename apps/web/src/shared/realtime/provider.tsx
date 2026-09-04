import { type ReactNode, useRef } from "react";

import { RealtimeContext } from "@/shared/realtime/context";
import { RealtimeClient } from "@/shared/realtime/socket-client";

/**
 * The client, as a component reaches it — A64-020.5B §3.
 *
 * ## Why the *lifecycle* is not here
 *
 * Starting the socket needs a session and stopping it needs a sign-out, and
 * `shared` may not import `features` (`specs/frontend.md` §3 — dependencies
 * point downward, enforced by `import/no-restricted-paths`). So this owns
 * the client and the subscription API; `app/providers/realtime-provider`
 * owns the wiring, which is composition and belongs in the layer that is
 * allowed to know about both.
 *
 * That split is not a workaround for the lint rule. It is the rule being
 * right: a transport that imported the session would be a transport that
 * could not be tested, reused or reasoned about without one.
 *
 * ## The context value never changes
 *
 * It holds the client instance and nothing else, created once with a ref.
 * §3 forbids a context value that causes broad re-renders; the way to
 * honour that is for the value to be referentially stable forever.
 * *Status* is read through `useSyncExternalStore`, which subscribes each
 * consumer individually and re-renders only those that asked.
 */
export function RealtimeContextProvider({
  children,
  client,
}: {
  children: ReactNode;
  /** Injectable so a test can drive a fake transport. */
  client?: RealtimeClient;
}) {
  const ref = useRef<RealtimeClient | null>(null);
  ref.current ??= client ?? new RealtimeClient();

  return <RealtimeContext value={ref.current}>{children}</RealtimeContext>;
}
