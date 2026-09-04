import { createContext, use, useEffect, useRef, useSyncExternalStore } from "react";

import type { ConnectionStatus } from "@/shared/realtime/connection-state";
import type { FrameListener } from "@/shared/realtime/socket-client";
import type { RealtimeClient } from "@/shared/realtime/socket-client";

/**
 * The realtime context, and the hooks that read it — A64-025.13B §37.
 *
 * Split from the provider for the reason `shared/i18n/context` records: a
 * module that creates a context **and** exports a component can be
 * hot-swapped by Fast Refresh, and the swap gives every already-mounted
 * consumer a context object the new provider is not filling.
 *
 * These were the three `react-refresh/only-export-components` warnings the
 * repository had been carrying — the rule describing exactly that hazard,
 * reported and left. Nothing here is a component, so Fast Refresh will not
 * swap this module.
 */
export const RealtimeContext = createContext<RealtimeClient | null>(null);

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
