/**
 * The realtime transport — AD-11's one socket per tab.
 *
 * Everything outside this directory reaches the gateway through these
 * exports and never constructs a frame: the protocol contract is
 * hand-maintained (`protocol.ts`) and confining it here is what makes it
 * reviewable against `app/gateway/protocol.py`.
 */
export type { ConnectionStatus, DeliveryMode } from "./connection-state";
export { deliveryMode, isPursuing, isReady, isTerminal } from "./connection-state";
export {
  RealtimeContextProvider,
  useConnectionStatus,
  useFrames,
  useRealtime,
} from "./context";
export type {
  AppliedMove,
  ClockPayload,
  CommandRejectedPayload,
  DrawDeclinedPayload,
  DrawOffer,
  DrawOfferedPayload,
  DrawState,
  DrawStatePayload,
  GameCommandPayload,
  GameCommandType,
  GameCompletedPayload,
  GatewayErrorCode,
  InboundFrame,
  MatchOfferedPayload,
  MovePayload,
  NotificationCreatedPayload,
  PlacedPiece,
  Rank,
  ResultPayload,
  Side,
  SnapshotPayload,
} from "./protocol";
export { RealtimeError } from "./request-registry";
export type { FrameListener } from "./socket-client";
export { RealtimeClient } from "./socket-client";
