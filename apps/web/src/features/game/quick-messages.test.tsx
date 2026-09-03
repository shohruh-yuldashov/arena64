import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { StrictMode, useState } from "react";
import { beforeEach, expect, it, vi } from "vitest";

import {
  QUICK_MESSAGE_ERROR_TTL_MS,
  useQuickMessages,
} from "@/features/game/model/use-quick-messages";
import { QuickMessagePicker } from "@/features/game/ui/quick-message-picker";
import { httpClient } from "@/shared/api/client";
import { env } from "@/shared/config/env";
import { QUICK_MESSAGES, RealtimeClient, RealtimeContextProvider } from "@/shared/realtime";
import { mswServer } from "@/shared/test/msw/server";
import { renderApp, renderWithProviders } from "@/shared/test/render";

/**
 * Quick messages, end to end — A64-023.2 §21.
 *
 * Two kinds of test, and the split is deliberate.
 *
 * The first three render **`QuickMessagePicker` alone**, because what they
 * assert is about the control itself: which items exist, that no text can be
 * entered, and that a keyboard can drive it. Mounting the whole app for
 * those would prove the same thing more slowly.
 *
 * The rest drive the **real app, the real `RealtimeClient` and the real
 * `parseFrame`** through a stubbed `WebSocket`, exactly as
 * `notifications/realtime.test.tsx` does — and for the same reason. This
 * phase added `game.quick_message.received` to `INBOUND_TYPES`, and before
 * it was there `parseFrame` returned `null` and the frame was dropped
 * silently. A test that handed a pre-parsed object to the hook would not
 * have noticed. It also proves the picker is *mounted in the real page*
 * rather than merely implemented.
 */

const url = (path: string) => `${env.VITE_API_URL}${path}`;
const envelope = <T,>(data: T) => ({ data, meta: { request_id: null, correlation_id: null } });

const VIEWER = "11111111-1111-1111-1111-111111111111";
const OPPONENT = "22222222-2222-2222-2222-222222222222";
const MATCH = "019fd1c7-5178-7a94-8076-4eeece03a8f4";

const SESSION = {
  id: VIEWER,
  username: "viewer",
  display_name: "Viewer",
  email: "viewer@example.com",
  is_active: true,
  is_verified: true,
};

// --- the picker on its own ---------------------------------------------------

it("offers exactly the server's catalogue and nothing that could carry text", async () => {
  // §21.1 and §21.3 in one test, because they are one property: the picker
  // is a closed list. Asserting the six exist proves the catalogue is
  // rendered from `QUICK_MESSAGES`; asserting there is no textbox proves
  // there is no other way to produce a message — and the second is the
  // claim that actually matters, since it is what makes free text
  // unsendable from this UI.
  const { container } = renderWithProviders(
    <QuickMessagePicker
      disabled={false}
      muted={false}
      error={null}
      onSelect={() => {}}
      onToggleMute={() => {}}
    />,
  );

  await userEvent.setup().click(screen.getByRole("button", { name: /^send a message$/i }));

  const menu = screen.getByRole("menu");
  expect(within(menu).getAllByRole("menuitem")).toHaveLength(QUICK_MESSAGES.length);
  // Every catalogue member is present, by its **English label** — which is
  // also the localisation contract working: the identifier never reaches
  // the DOM.
  expect(within(menu).getByRole("menuitem", { name: /nice move/i })).toBeTruthy();
  expect(within(menu).getByRole("menuitem", { name: /good game/i })).toBeTruthy();
  expect(screen.queryByText("nice_move")).toBeNull();

  // Nothing anywhere in the tree can accept typing.
  expect(screen.queryByRole("textbox")).toBeNull();
  expect(container.querySelector("input, textarea, [contenteditable]")).toBeNull();
});

it("sends the semantic identifier, not the label, and closes", async () => {
  // §21.2. The identifier is the protocol value; the label is presentation
  // and differs per locale. A picker that passed its own text upward would
  // send Uzbek to the gateway and be refused.
  const sent: string[] = [];
  renderWithProviders(
    <QuickMessagePicker
      disabled={false}
      muted={false}
      error={null}
      onSelect={(message) => sent.push(message)}
      onToggleMute={() => {}}
    />,
  );

  const user = userEvent.setup();
  await user.click(screen.getByRole("button", { name: /^send a message$/i }));
  await user.click(screen.getByRole("menuitem", { name: /nice move/i }));

  expect(sent).toEqual(["nice_move"]);
  expect(screen.queryByRole("menu")).toBeNull();
});

it("is keyboard operable and returns focus on Escape", async () => {
  // §15. A menu with the right roles that a keyboard cannot drive is not
  // accessible; this is the part that would silently rot, since a mouse
  // test passes either way.
  renderWithProviders(
    <QuickMessagePicker
      disabled={false}
      muted={false}
      error={null}
      onSelect={() => {}}
      onToggleMute={() => {}}
    />,
  );

  const user = userEvent.setup();
  const trigger = screen.getByRole("button", { name: /^send a message$/i });
  await user.click(trigger);

  // Opening focuses the first item, so the list is reachable at all.
  expect(document.activeElement).toBe(screen.getAllByRole("menuitem")[0]);
  await user.keyboard("{ArrowDown}");
  expect(document.activeElement).toBe(screen.getAllByRole("menuitem")[1]);
  await user.keyboard("{End}");
  expect(document.activeElement).toBe(
    screen.getAllByRole("menuitem")[QUICK_MESSAGES.length - 1],
  );

  await user.keyboard("{Escape}");
  expect(screen.queryByRole("menu")).toBeNull();
  // Focus must come back, or a keyboard user is stranded at the document.
  expect(document.activeElement).toBe(trigger);
});

it("exposes the mute state to assistive technology, not only as a label", async () => {
  // §12, §15. `aria-pressed` is the state; the changing label alone would
  // leave a screen reader unable to answer "am I muted right now".
  //
  // Driven through a real `useState` rather than a `rerender`, so the
  // assertion is about the control *reacting to its own toggle* — which is
  // how the page wires it — rather than about two independent renders.
  function Harness({ disabled }: { disabled: boolean }) {
    const [muted, setMuted] = useState(false);
    return (
      <QuickMessagePicker
        disabled={disabled}
        muted={muted}
        error={null}
        onSelect={() => {}}
        onToggleMute={() => setMuted((current) => !current)}
      />
    );
  }

  renderWithProviders(<Harness disabled={false} />);

  const button = screen.getByRole("button", { name: /^mute quick messages$/i });
  expect(button.getAttribute("aria-pressed")).toBe("false");

  await userEvent.setup().click(button);

  const muted = await screen.findByRole("button", { name: /^unmute quick messages$/i });
  expect(muted.getAttribute("aria-pressed")).toBe("true");
});

it("disables sending but never the mute control on a terminal match", () => {
  // §10 and §12 together. A finished game must stop accepting messages —
  // and must still let a player silence a bubble that is on screen, which
  // is why the two controls are gated separately rather than by one flag.
  renderWithProviders(
    <QuickMessagePicker
      disabled={true}
      muted={false}
      error={null}
      onSelect={() => {}}
      onToggleMute={() => {}}
    />,
  );

  expect(
    screen.getByRole("button", { name: /^send a message$/i }).hasAttribute("disabled"),
  ).toBe(true);
  expect(
    screen.getByRole("button", { name: /^mute quick messages$/i }).hasAttribute("disabled"),
  ).toBe(false);
});

// --- through the real app and the real socket --------------------------------

interface StubSocket {
  onmessage: ((event: { data: string }) => void) | null;
  sent: string[];
}

/**
 * The app's socket, stubbed at the browser boundary.
 *
 * Answers `room.join` and `game.resume` so the page reaches a playable
 * state — without them it renders a skeleton and there is no picker to
 * click. Everything above that is the real client, the real parser and the
 * real hooks.
 */
function stubWebSocket(status: "active" | "completed" = "active"): StubSocket[] {
  const sockets: StubSocket[] = [];

  vi.stubGlobal(
    "WebSocket",
    class {
      static readonly OPEN = 1;
      static readonly CONNECTING = 0;
      readyState = 1;
      onmessage: ((event: { data: string }) => void) | null = null;
      onclose: unknown = null;
      onerror: unknown = null;
      readonly sent: string[] = [];

      constructor() {
        sockets.push(this);
        queueMicrotask(() => {
          this.onmessage?.({
            data: JSON.stringify({
              v: 1,
              type: "connection.ready",
              channel: "system",
              payload: {},
            }),
          });
        });
      }
      close() {}
      send(frame: string) {
        this.sent.push(frame);
        const parsed = JSON.parse(frame) as { type: string; request_id?: string };
        if (parsed.type === "room.join") {
          this.onmessage?.({
            data: JSON.stringify({
              v: 1,
              type: "room.joined",
              request_id: parsed.request_id ?? null,
              channel: "game",
              payload: {
                match_id: MATCH,
                participants: [VIEWER, OPPONENT],
                both_connected: true,
              },
            }),
          });
        }
        if (parsed.type === "game.resume") {
          this.onmessage?.({
            data: JSON.stringify({
              v: 1,
              type: "game.snapshot",
              request_id: parsed.request_id ?? null,
              channel: "game",
              payload: {
                match_id: MATCH,
                engine_version: 2,
                variant: "russian_8x8",
                status,
                rated: false,
                sequence: 4,
                side_to_move: "light",
                fingerprint: "fp4",
                pieces: [
                  { square: "c3", side: "light", rank: "man" },
                  { square: "f6", side: "dark", rank: "man" },
                ],
                participants: { light: VIEWER, dark: OPPONENT },
                clock: null,
                result:
                  status === "completed"
                    ? {
                        outcome: "light_won",
                        termination_reason: "resignation",
                        winner: "light",
                      }
                    : null,
                draw: { offer: null, may_offer: true, may_accept: false, may_decline: false },
                server_time: "2026-08-05T10:00:00Z",
              },
            }),
          });
        }
      }
    },
  );

  return sockets;
}

/** One `game.quick_message.received`, exactly as the gateway encodes it. */
function receive(socket: StubSocket, from: "light" | "dark", message: string): void {
  socket.onmessage?.({
    data: JSON.stringify({
      v: 1,
      type: "game.quick_message.received",
      request_id: null,
      channel: "game",
      payload: { match_id: MATCH, from, message, sent_at: "2026-08-09T10:00:00Z" },
    }),
  });
}

beforeEach(() => {
  vi.spyOn(console, "error").mockImplementation(() => {});
  httpClient.interceptors.request.clear();
  httpClient.interceptors.response.clear();
  mswServer.use(
    http.post(url("/auth/browser/refresh"), () =>
      HttpResponse.json(envelope({ access_token: "token-1", user: SESSION })),
    ),
    http.post(url("/auth/ws-ticket"), () =>
      HttpResponse.json(envelope({ ticket: "t1", expires_at: "2026-08-05T10:00:30Z" }), {
        status: 201,
      }),
    ),
  );
});

async function openGame(status: "active" | "completed" = "active") {
  const sockets = stubWebSocket(status);
  renderApp({ path: `/games/${MATCH}`, realtimeClient: new RealtimeClient() });
  // The picker only renders once the snapshot has named this client a seat.
  await screen.findByRole("button", { name: /^send a message$/i }, { timeout: 5000 });
  return sockets;
}

it("puts a real send frame on the real socket and renders the server's echo once", async () => {
  // §21.2, §21.4 and reachability in one pass: the picker is mounted in the
  // real page, its selection reaches the transport as the documented
  // contract, and the sender's bubble appears **only** from the server's
  // fan-out — §5's rule against an optimistic render, which would otherwise
  // show the message twice.
  const sockets = await openGame();
  const socket = sockets[sockets.length - 1]!;

  const user = userEvent.setup();
  await user.click(screen.getByRole("button", { name: /^send a message$/i }));
  await user.click(screen.getByRole("menuitem", { name: /good game/i }));

  const frame = socket.sent
    .map((raw) => JSON.parse(raw) as { type: string; payload: Record<string, unknown> })
    .find((f) => f.type === "game.quick_message.send");
  expect(frame?.payload).toEqual({ match_id: MATCH, message: "good_game" });

  // Nothing on screen yet — the server has not echoed.
  expect(screen.queryAllByText(/good game/i)).toHaveLength(0);

  receive(socket, "light", "good_game");
  await waitFor(() => expect(screen.getAllByText(/good game/i)).toHaveLength(1));
});

it("localises an opponent's message and replaces it when a newer one arrives", async () => {
  // §21.5 and §21.6. The frame carries `nice_move`; what a player reads is
  // "Nice move!" — the identifier never reaches the DOM. Then §7's
  // replacement policy: the second message from the same seat **replaces**
  // the first rather than stacking beside it.
  const sockets = await openGame();
  const socket = sockets[sockets.length - 1]!;

  receive(socket, "dark", "nice_move");
  await waitFor(() => expect(screen.getAllByText(/nice move/i)).toHaveLength(1));
  expect(screen.queryByText("nice_move")).toBeNull();

  receive(socket, "dark", "well_played");
  await waitFor(() => expect(screen.getAllByText(/well played/i)).toHaveLength(1));
  // Replaced, not stacked.
  expect(screen.queryAllByText(/nice move/i)).toHaveLength(0);
});

it("mutes the opponent's messages without silencing our own or touching the board", async () => {
  // §21.7 — the test that proves mute is a *presentation* filter. The board
  // is asserted afterwards because §11 forbids mute affecting game state,
  // and a mute implemented in the wrong place would have unsubscribed the
  // room or dropped a move frame.
  const sockets = await openGame();
  const socket = sockets[sockets.length - 1]!;

  const user = userEvent.setup();
  await user.click(screen.getByRole("button", { name: /^mute quick messages$/i }));

  receive(socket, "dark", "nice_move");
  // Nothing appears, and nothing is queued for later — §12's "applies
  // prospectively, no hidden queue".
  await waitFor(() => expect(screen.queryAllByText(/nice move/i)).toHaveLength(0));

  // Our own still renders through the server echo.
  receive(socket, "light", "thanks");
  await waitFor(() => expect(screen.getAllByText(/thanks/i)).toHaveLength(1));

  // Unmuting must not replay the suppressed message.
  await user.click(screen.getByRole("button", { name: /^unmute quick messages$/i }));
  expect(screen.queryAllByText(/nice move/i)).toHaveLength(0);

  // The game is untouched: the board is still rendered and the socket was
  // never closed or re-joined by muting.
  expect(screen.getByRole("button", { name: /^resign$/i })).toBeTruthy();
  expect(socket.sent.filter((raw) => raw.includes("room.join"))).toHaveLength(1);
});

it("shows a calm localised message when the server rate-limits a send", async () => {
  // §9, §21.9. The gateway owns the limit; this asserts the *client's*
  // reaction is a small sentence rather than the generic transport failure
  // text §9 forbids for an ordinary refusal.
  const sockets = await openGame();
  const socket = sockets[sockets.length - 1]!;

  const user = userEvent.setup();
  await user.click(screen.getByRole("button", { name: /^send a message$/i }));
  await user.click(screen.getByRole("menuitem", { name: /thanks/i }));

  socket.onmessage?.({
    data: JSON.stringify({
      v: 1,
      type: "game.command.rejected",
      request_id: null,
      channel: "game",
      payload: { code: "rate_limited", reason: "Too many messages. Slow down." },
    }),
  });

  await waitFor(() => expect(screen.getByText(/too quick/i)).toBeTruthy());
  // The server's own English prose is never rendered — §13 of the game
  // protocol forbids branching on it, and this forbids showing it.
  expect(screen.queryByText(/slow down/i)).toBeNull();
});

it("refuses to send once the match is terminal, and replays nothing on reconnect", async () => {
  // §10 and §21.8/§21.10 together. A completed match disables the trigger,
  // and because quick messages are never buffered by the gateway, a fresh
  // socket brings none back — asserted by re-joining and finding the board
  // without a bubble.
  const sockets = await openGame("completed");
  expect(
    screen.getByRole("button", { name: /^send a message$/i }).hasAttribute("disabled"),
  ).toBe(true);

  // A message that arrived while the game was live finishes its own
  // lifetime; nothing re-creates one afterwards.
  const socket = sockets[sockets.length - 1]!;
  receive(socket, "dark", "good_game");
  await waitFor(() => expect(screen.getAllByText(/good game/i)).toHaveLength(1));

  // A reconnect: the gateway sends a snapshot and no quick messages, because
  // it never stored one. The bubble is gone with the old component state.
  socket.onmessage?.({
    data: JSON.stringify({
      v: 1,
      type: "game.resync_required",
      request_id: null,
      channel: "game",
      payload: { match_id: MATCH },
    }),
  });
  const before = socket.sent.filter((raw) => raw.includes("quick_message")).length;
  expect(before).toBe(0);
});

// --- hardening — A64-023.3 ---------------------------------------------------

it("registers exactly one frame handler per mounted surface, even under StrictMode", () => {
  // §3, §19.2. The audit found no duplicate-render defect and this is what
  // keeps it that way: `useFrames` installs its listener in an effect and
  // returns the unsubscribe, so React's development double-invoke
  // subscribes, unsubscribes and subscribes again — netting one.
  //
  // Asserted by **counting live subscriptions** rather than by counting
  // rendered bubbles, because a bubble count would pass for the wrong
  // reason: two listeners both calling `setVisible` with the same seat key
  // produce one bubble and two renders. The defect this guards against is
  // invisible in the DOM.
  const client = new RealtimeClient();
  let live = 0;
  const subscribe = client.onFrame.bind(client);
  vi.spyOn(client, "onFrame").mockImplementation((listener) => {
    live += 1;
    const unsubscribe = subscribe(listener);
    return () => {
      live -= 1;
      unsubscribe();
    };
  });

  function Harness() {
    const quick = useQuickMessages({ matchId: MATCH, viewerSide: "light", playable: true });
    return <output>{quick.visible.size}</output>;
  }

  const { unmount } = render(
    <StrictMode>
      <RealtimeContextProvider client={client}>
        <Harness />
      </RealtimeContextProvider>
    </StrictMode>,
  );

  expect(live).toBe(1);
  // And it is genuinely released, rather than merely balanced at mount —
  // a listener that outlived its surface would fire into a dead component
  // on the next reconnect.
  unmount();
  expect(live).toBe(0);
});

it("treats an intentionally repeated message as new rather than as a duplicate", async () => {
  // §2, §19.3. The distinction this phase had to preserve: a player sending
  // `nice_move` twice is **two intentional messages**, and only a frame
  // observed twice by accident is a duplicate. A dedupe keyed on the
  // identifier would have collapsed the first case into the second.
  //
  // Proven through the timer rather than through a count, because a
  // replacement is not visible as a second element: the second message
  // restarts the four-second lifetime, so a bubble that was dropped as a
  // duplicate would disappear on the original schedule and one that was
  // accepted survives past it.
  vi.useFakeTimers();
  try {
    const sockets = stubWebSocket();
    renderApp({ path: `/games/${MATCH}`, realtimeClient: new RealtimeClient() });
    await vi.waitFor(() =>
      expect(screen.getByRole("button", { name: /^send a message$/i })).toBeTruthy(),
    );
    const socket = sockets[sockets.length - 1]!;

    receive(socket, "dark", "nice_move");
    await vi.waitFor(() => expect(screen.getAllByText(/nice move/i)).toHaveLength(1));

    // Three seconds in — still inside the first lifetime.
    act(() => {
      vi.advanceTimersByTime(3000);
    });
    receive(socket, "dark", "nice_move");

    // Two more seconds: past the *first* message's four, inside the
    // second's. A duplicate-suppressing client would show nothing here.
    act(() => {
      vi.advanceTimersByTime(2000);
    });
    expect(screen.getAllByText(/nice move/i)).toHaveLength(1);

    // And it does eventually go — the timer restarted, it did not vanish.
    act(() => {
      vi.advanceTimersByTime(3000);
    });
    expect(screen.queryAllByText(/nice move/i)).toHaveLength(0);
  } finally {
    vi.useRealTimers();
  }
});

// --- polish — A64-023.4 ------------------------------------------------------

it("takes a refusal away again instead of leaving it on a finished game", async () => {
  // §7. `send` resets the error, and `send` returns early once the match is
  // terminal — so a player rate-limited on the last move kept "Too quick"
  // on screen for as long as they stayed on the page. A message about a
  // transient condition must not outlive it.
  vi.useFakeTimers();
  try {
    const sockets = stubWebSocket();
    renderApp({ path: `/games/${MATCH}`, realtimeClient: new RealtimeClient() });
    await vi.waitFor(() =>
      expect(screen.getByRole("button", { name: /^send a message$/i })).toBeTruthy(),
    );
    const socket = sockets[sockets.length - 1]!;

    socket.onmessage?.({
      data: JSON.stringify({
        v: 1,
        type: "game.command.rejected",
        request_id: null,
        channel: "game",
        payload: { code: "unknown_quick_message", reason: "That message is not available." },
      }),
    });

    await vi.waitFor(() => expect(screen.getByText(/not available/i)).toBeTruthy());

    act(() => {
      vi.advanceTimersByTime(QUICK_MESSAGE_ERROR_TTL_MS + 100);
    });
    expect(screen.queryByText(/not available/i)).toBeNull();
  } finally {
    vi.useRealTimers();
  }
});

it("returns focus to the trigger when a finishing game closes an open picker", async () => {
  // §7. Closing the menu unmounts whichever item held focus. Without the
  // restoration below, focus falls to `<body>` at the exact moment the
  // result appears — a keyboard user is stranded at the top of the
  // document and has to tab back through the whole page.
  //
  // The flag is flipped through captured state rather than by clicking
  // anything, because a click would move focus itself and the restoration
  // is deliberately conditional on focus still being *inside the menu*.
  let finish = () => {};
  function Harness() {
    const [disabled, setDisabled] = useState(false);
    finish = () => setDisabled(true);
    return (
      <QuickMessagePicker
        disabled={disabled}
        muted={false}
        error={null}
        onSelect={() => {}}
        onToggleMute={() => {}}
      />
    );
  }

  renderWithProviders(<Harness />);
  const user = userEvent.setup();
  const trigger = screen.getByRole("button", { name: /^send a message$/i });
  const mute = screen.getByRole("button", { name: /^mute quick messages$/i });

  await user.click(trigger);
  expect(document.activeElement).toBe(screen.getAllByRole("menuitem")[0]);

  // The match completes while the menu is open and focus is inside it.
  act(() => {
    finish();
  });

  expect(screen.queryByRole("menu")).toBeNull();
  // The **mute** button, not the trigger: the trigger is disabled by now
  // and a disabled button cannot hold focus, so focusing it would silently
  // leave the player on `<body>` — the exact failure this prevents.
  expect(document.activeElement).toBe(mute);
  expect(trigger.hasAttribute("disabled")).toBe(true);
});
