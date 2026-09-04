import { render } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { GameplayPreferences } from "@/features/preferences/gameplay-preferences";

vi.mock("@/features/profile/model/queries", () => ({ usePreferences: vi.fn() }));
const { usePreferences } = await import("@/features/profile/model/queries");

/**
 * The preferences the document itself honours — A64-025.12 §34.4.
 *
 * ## Why this test did not exist until now
 *
 * `board_theme`, `piece_set` and `show_coordinates` were closed in .5B and
 * .6D and **nothing covered the component that closes them**. Every test in
 * the product would still pass if this effect stopped writing its
 * attributes: the CSS would be correct, the setting would save, and the
 * board would silently stop changing — which is exactly the write-only
 * state those phases existed to end.
 *
 * `animation_speed` is the fourth, and it is the one where the failure is
 * not cosmetic: a player who set `instant` because motion gives them
 * migraines gets motion.
 *
 * The query is mocked rather than served, because what is under test is the
 * mapping from a preference to an attribute and the cleanup that follows a
 * sign-out. Serving it would test TanStack Query.
 */

function mountWith(gameplay: Record<string, unknown> | undefined) {
  vi.mocked(usePreferences).mockReturnValue({
    data: gameplay === undefined ? undefined : { gameplay },
  } as unknown as ReturnType<typeof usePreferences>);
  return render(<GameplayPreferences />);
}

describe("the gameplay preferences", () => {
  beforeEach(() => {
    for (const key of ["boardTheme", "pieceSet", "coordinates", "motion"]) {
      delete document.documentElement.dataset[key];
    }
  });

  it("puts every preference the stylesheet reads on the document", () => {
    mountWith({
      board_theme: "wood",
      piece_set: "neo",
      show_coordinates: false,
      animation_speed: "instant",
    });

    const { dataset } = document.documentElement;
    expect(dataset.boardTheme).toBe("wood");
    expect(dataset.pieceSet).toBe("neo");
    expect(dataset.coordinates).toBe("off");
    expect(dataset.motion).toBe("instant");
  });

  it("writes nothing before the preferences have arrived", () => {
    // An attribute written from `undefined` would be the string
    // "undefined", and `[data-motion="undefined"]` matches no rule — the
    // player would get the default silently instead of their choice a
    // moment later.
    mountWith(undefined);

    expect(document.documentElement.dataset.motion).toBeUndefined();
    expect(document.documentElement.dataset.boardTheme).toBeUndefined();
  });

  it("clears the document when it unmounts, which is what signing out does", () => {
    // Leaving the last player's settings on the document would show the
    // next person who signs in on this browser a board they never chose —
    // and, for `animation_speed`, motion they may have switched off.
    const { unmount } = mountWith({
      board_theme: "midnight",
      piece_set: "modern",
      show_coordinates: true,
      animation_speed: "slow",
    });

    expect(document.documentElement.dataset.motion).toBe("slow");
    unmount();

    const { dataset } = document.documentElement;
    expect(dataset.motion).toBeUndefined();
    expect(dataset.boardTheme).toBeUndefined();
    expect(dataset.pieceSet).toBeUndefined();
    expect(dataset.coordinates).toBeUndefined();
  });
});
