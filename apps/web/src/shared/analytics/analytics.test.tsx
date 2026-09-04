import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { StrictMode, useState } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { CLIENT_EVENT_NAMES } from "@/shared/analytics/events";
import {
  ANONYMOUS_ID_TTL_MS,
  anonymousId,
  rotateAnonymousId,
} from "@/shared/analytics/identity";
import { flush, resetTracker, track } from "@/shared/analytics/tracker";
import { useViewEvent } from "@/shared/analytics/use-view-event";

/**
 * The tracker's three contracts — A64-027.2 §36, §37, §61.
 *
 * Each is a way behavioural measurement goes wrong invisibly: a view
 * counted twice, a navigation blocked by a failed request, an identity
 * that outlives a sign-out.
 */

let sent: Blob[] = [];

/** The bodies `sendBeacon` was handed. `Blob.text()` is async in jsdom. */
async function bodies(): Promise<
  { events: { event_name: string; properties: Record<string, unknown> }[] }[]
> {
  return Promise.all(sent.map(async (blob) => JSON.parse(await blob.text())));
}

beforeEach(() => {
  sent = [];
  resetTracker();
  localStorage.clear();
  sessionStorage.clear();
  // `sendBeacon` is the real transport. Stubbed to record rather than to
  // succeed silently, so a test can assert what would have been sent.
  Object.defineProperty(navigator, "sendBeacon", {
    value: (_url: string, blob: Blob) => {
      sent.push(blob);
      return true;
    },
    configurable: true,
  });
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("the browser identity", () => {
  it("is stable across calls", () => {
    expect(anonymousId()).toBe(anonymousId());
  });

  it("rotates after thirty days", () => {
    // D4, and clock-testable rather than waiting a month.
    const first = anonymousId(0);
    expect(anonymousId(ANONYMOUS_ID_TTL_MS - 1)).toBe(first);
    expect(anonymousId(ANONYMOUS_ID_TTL_MS + 1)).not.toBe(first);
  });

  it("rotates on sign-out", () => {
    // Keeping it would attach the next person on a shared computer to the
    // previous one's visit.
    const before = anonymousId();
    expect(rotateAnonymousId()).not.toBe(before);
    expect(anonymousId()).not.toBe(before);
  });

  it("survives storage being unavailable", () => {
    // A private window with site data blocked throws on every access. An
    // analytics identity is never worth an exception on a page load.
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("denied");
    });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("denied");
    });

    expect(() => anonymousId()).not.toThrow();
    expect(anonymousId()).toMatch(/^[0-9a-f-]{36}$/);
  });
});

describe("the tracker", () => {
  it("never throws, whatever the transport does", () => {
    // §61: a failure here would reach a click handler and stop the
    // navigation the click was for.
    Object.defineProperty(navigator, "sendBeacon", {
      value: () => {
        throw new Error("blocked by an extension");
      },
      configurable: true,
    });

    expect(() => {
      track("landing_viewed", {});
      flush();
    }).not.toThrow();
  });

  it("sends nothing when nothing was tracked", () => {
    flush();
    expect(sent).toEqual([]);
  });

  it("batches rather than sending one request per event", () => {
    track("landing_viewed", {});
    track("register_cta_clicked", { placement: "hero" });
    flush();

    expect(sent).toHaveLength(1);
  });
});

describe("view events", () => {
  function Probe({ id, label }: { id: string; label: string }) {
    useViewEvent("public_tournament_viewed", { tournament_id: id, status: "in_progress" }, id);
    return <span>{label}</span>;
  }

  function Rerenderer({ id }: { id: string }) {
    const [count, setCount] = useState(0);
    return (
      <>
        <button type="button" onClick={() => setCount((n) => n + 1)}>
          rerender
        </button>
        <Probe id={id} label={`rendered ${String(count)}`} />
      </>
    );
  }

  it("fires once even under StrictMode's double mount", async () => {
    // The defect this hook exists for: development mounts, unmounts and
    // remounts every component, so a naive effect counts every view twice
    // — and the number looks plausible.
    render(
      <StrictMode>
        <Probe id="t1" label="one" />
      </StrictMode>,
    );
    flush();

    const events = (await bodies()).flatMap((body) => body.events);
    expect(events.filter((e) => e.event_name === "public_tournament_viewed")).toHaveLength(1);
  });

  it("does not fire again on a rerender", async () => {
    render(<Rerenderer id="t1" />);
    await userEvent.click(screen.getByRole("button", { name: "rerender" }));
    await userEvent.click(screen.getByRole("button", { name: "rerender" }));
    flush();

    const events = (await bodies()).flatMap((body) => body.events);
    expect(events).toHaveLength(1);
  });

  it("fires again for a different tournament", async () => {
    // A changed key is a new view. Collapsing these would undercount
    // somebody who really did open two pages.
    const { rerender } = render(<Probe id="t1" label="one" />);
    rerender(<Probe id="t2" label="two" />);
    flush();

    const events = (await bodies()).flatMap((body) => body.events);
    expect(events).toHaveLength(2);
    expect(events.map((e) => e.properties.tournament_id)).toEqual(["t1", "t2"]);
  });
});

describe("the taxonomy", () => {
  it("names exactly the four events the server accepts", () => {
    // Asserted against a written-out list rather than derived, so this
    // disagrees with the code when the code changes — which is the only
    // way it can catch a fifth name appearing.
    expect([...CLIENT_EVENT_NAMES].sort()).toEqual([
      "landing_viewed",
      "public_tournament_viewed",
      "register_cta_clicked",
      "share_clicked",
    ]);
  });
});
