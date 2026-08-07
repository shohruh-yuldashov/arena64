import { describe, expect, it } from "vitest";

import { NOTIFICATION_ROUTES } from "@/shared/config/notification-routes";

import { isPushPayload, presentationFor } from "./push-presentation";

/**
 * What a push says and where it goes — A64-021.6 §12, §13, §30.
 *
 * Directly, against the pure functions, for the reason `cache-policy.test.ts`
 * exists: these are the two security-relevant decisions in the push path,
 * and they must be testable without a `ServiceWorkerGlobalScope` to fake or
 * a bundled artefact to evaluate.
 *
 * The claims are the ones §13 asks for and one §12 does:
 *
 *   every destination is a **same-origin path from a closed table**, and no
 *     payload value can become one;
 *   an unknown type still renders something and still navigates somewhere,
 *     because a push that displays nothing cannot be reported;
 *   nothing private is in the text, because the text is compiled in.
 */

const NOTIFICATION_ID = "019fb9ea-0a0c-7cec-9c5f-402727c31a96";

describe("what a push notification says", () => {
  it("renders a known type from the compiled table", () => {
    const presentation = presentationFor({
      n: NOTIFICATION_ID,
      t: "tournament_round_published",
    });

    expect(presentation.title).toBe("A new round is live");
    expect(presentation.path).toBe("/tournaments");
    // The **type**, not the id — three rounds published while a phone slept
    // should replace each other rather than stack, which is what makes push
    // survivable.
    expect(presentation.tag).toBe("tournament_round_published");
  });

  it("still shows something for a type this build has never heard of", () => {
    // The backend may add a type before a deployed frontend knows it. That
    // must not be a push that displays nothing: browsers penalise a worker
    // which receives a push and shows no notification, and a person cannot
    // report a notification they never saw.
    const presentation = presentationFor({ n: NOTIFICATION_ID, t: "invented_next_quarter" });

    expect(presentation.title).toBe("Arena64");
    expect(presentation.path).toBe("/notifications");
  });

  it("says nothing about the tournament, the opponent or the person", () => {
    // §11, §12. The payload carries two identifiers and this table carries
    // fixed sentences, so there is no path by which a name reaches a lock
    // screen. Asserted over **every** entry rather than one, because the
    // rule is about the table and not about a case.
    const types = [
      "tournament_round_published",
      "tournament_registration_confirmed",
      "tournament_completed",
      "friend_request_received",
      "friend_request_accepted",
      "unknown",
    ];

    for (const type of types) {
      const { title, body } = presentationFor({ n: NOTIFICATION_ID, t: type });
      expect(`${title} ${body}`).not.toContain(NOTIFICATION_ID);
    }
  });
});

describe("the social types — A64-021.6A", () => {
  it("says a request arrived without saying who sent it", () => {
    // **§4.** The strictest rule in the table, and stricter than the
    // tournament entries follow. A tournament notification discloses that
    // somebody plays in tournaments; a social one would disclose *who is
    // trying to reach them*, on a lock screen, to whoever is looking.
    const received = presentationFor({ n: NOTIFICATION_ID, t: "friend_request_received" });
    const accepted = presentationFor({ n: NOTIFICATION_ID, t: "friend_request_accepted" });

    expect(received.title).toBe("Arena64");
    expect(received.body).toBe("You have a new friend request.");
    expect(accepted.title).toBe("Arena64");
    expect(accepted.body).toBe("Your friend request was accepted.");
  });

  it("routes each one to the page that answers it", () => {
    // **§5**, and through the shared constants rather than string literals:
    // the in-app list resolves the same destinations through the same
    // values, so a copy here that drifted would be a push whose text says
    // "friend request" and whose tap opens a different page.
    expect(presentationFor({ n: NOTIFICATION_ID, t: "friend_request_received" }).path).toBe(
      NOTIFICATION_ROUTES.friendRequests,
    );
    expect(presentationFor({ n: NOTIFICATION_ID, t: "friend_request_accepted" }).path).toBe(
      NOTIFICATION_ROUTES.friends,
    );
  });
});

describe("where a tap goes", () => {
  it("never produces anything but an in-app absolute path", () => {
    // §13's "no arbitrary external URLs", asserted as a property. A payload
    // is not trusted input in practice — the browser decrypted it from bytes
    // this platform encrypted — but the guarantee must not depend on that,
    // because the day it does is the day a payload is composed somewhere
    // new.
    const hostile = [
      "https://evil.example.com",
      "//evil.example.com",
      "javascript:alert(1)",
      "../../etc/passwd",
      "",
    ];

    for (const type of hostile) {
      const { path } = presentationFor({ n: NOTIFICATION_ID, t: type });
      expect(path.startsWith("/")).toBe(true);
      expect(path.startsWith("//")).toBe(false);
      expect(new URL(path, "https://arena64.gg").origin).toBe("https://arena64.gg");
    }
  });
});

describe("narrowing a payload", () => {
  it("accepts what the backend sends and refuses everything else", () => {
    expect(isPushPayload({ n: NOTIFICATION_ID, t: "tournament_completed" })).toBe(true);
    // Each of these has reached a worker somewhere: a service that delivered
    // an empty push, a payload that was a bare string, one whose fields were
    // renamed. None may index into the table.
    expect(isPushPayload(null)).toBe(false);
    expect(isPushPayload("tournament_completed")).toBe(false);
    expect(isPushPayload({ notification_id: NOTIFICATION_ID })).toBe(false);
    expect(isPushPayload({ n: 1, t: 2 })).toBe(false);
  });
});
