import { createContext, type ReactNode, use, useRef, useSyncExternalStore } from "react";
import { useEffect } from "react";

import type { ConnectionStatus } from "@/shared/realtime/connection-state";
import type { FrameListener } from "@/shared/realtime/socket-client";
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
const RealtimeContext = createContext<RealtimeClient | null>(null);

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

export function useRealtime(): RealtimeClient {
  const client = use(RealtimeContext);
  if (client === null) {
    throw new Error("useRealtime must be used inside a RealtimeProvider.");
  }
  return client;
}

/**
 * The connection status, as a component may read it.
 *
 * `useSyncExternalStore` rather than a context value, so a status change
 * re-renders the components that asked for it and nothing else. §5's
 * "expose only stable derived state" is this: a consumer gets one string
 * and cannot reach the socket, the attempt counter or an error object.
 */
export function useConnectionStatus(): ConnectionStatus {
  const realtime = useRealtime();
  return useSyncExternalStore(
    (notify) => realtime.onStatus(notify),
    () => realtime.currentStatus,
    () => "idle" as const,
  );
}

/**
 * Subscribes to every inbound frame for as long as the component is mounted.
 *
 * The listener is held in a ref so a caller may pass an inline function
 * without resubscribing on every render — which would otherwise unsubscribe
 * and resubscribe between a frame arriving and being handled.
 */
export function useFrames(listener: FrameListener): void {
  const realtime = useRealtime();
  const held = useRef(listener);
  held.current = listener;

  useEffect(() => realtime.onFrame((frame) => held.current(frame)), [realtime]);
}
