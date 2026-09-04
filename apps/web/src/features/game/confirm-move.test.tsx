import { act, render, screen } from "@testing-library/react";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";

import type { Square } from "@/entities/board";
import { useMoveConfirmation } from "@/features/game/model/use-move-confirmation";
import { PendingMove } from "@/features/game/ui/pending-move";
import { renderWithProviders } from "@/shared/test/render";

/**
 * The move a player has chosen but not yet played — A64-025.14 §38.
 *
 * `confirm_move` is the one gameplay preference that changes *when a move
 * leaves the browser* rather than how something looks, so what has to be
 * asserted is a negative: with the preference on, choosing a move must
 * **not** submit it. A rendering test cannot see that; these can.
 *
 * The hook is exercised through a host component rather than `renderHook`,
 * because what matters is how it behaves across re-renders — a staged move
 * surviving one, and being dropped when the position changes underneath it.
 */

const PATH: Square[] = ["c3", "d4"];

function Host({
  enabled,
  onSubmit,
}: {
  enabled: boolean;
  onSubmit: (path: readonly Square[]) => void;
}) {
  const [sequence, setSequence] = useState(1);
  const confirmation = useMoveConfirmation({ enabled, sequence, onSubmit });

  return (
    <div>
      <button onClick={() => void confirmation.stage(PATH)}>choose</button>
      <button onClick={() => setSequence((n) => n + 1)}>opponent moves</button>
      <span data-testid="staged">{confirmation.staged === null ? "none" : "staged"}</span>
      {confirmation.staged !== null && (
        <PendingMove
          onConfirm={confirmation.confirm}
          onCancel={confirmation.cancel}
          disabled={false}
        />
      )}
    </div>
  );
}

function choose() {
  act(() => screen.getByRole("button", { name: "choose" }).click());
}

describe("when confirmation is off", () => {
  it("hands the move straight back for the caller to submit", () => {
    // The page reads `stage`'s answer to decide, so `false` here is what
    // keeps the default path unchanged: choose, and the move goes.
    const onSubmit = vi.fn();
    renderWithProviders(<Host enabled={false} onSubmit={onSubmit} />);

    choose();

    expect(screen.getByTestId("staged")).toHaveTextContent("none");
    expect(onSubmit).not.toHaveBeenCalled();
  });
});

describe("when confirmation is on", () => {
  it("holds the move instead of playing it", () => {
    // The assertion the preference exists for. A player who mis-clicks a
    // destination has not yet played it.
    const onSubmit = vi.fn();
    renderWithProviders(<Host enabled onSubmit={onSubmit} />);

    choose();

    expect(screen.getByTestId("staged")).toHaveTextContent("staged");
    expect(onSubmit).not.toHaveBeenCalled();
    expect(screen.getByRole("status")).toBeVisible();
  });

  it("plays it once, when the player says so", () => {
    const onSubmit = vi.fn();
    renderWithProviders(<Host enabled onSubmit={onSubmit} />);
    choose();

    act(() => screen.getByRole("button", { name: /play move/i }).click());

    expect(onSubmit).toHaveBeenCalledExactlyOnceWith(PATH);
    // Cleared, so a second press cannot send it again — and the control is
    // gone, so there is nothing to press.
    expect(screen.getByTestId("staged")).toHaveTextContent("none");
  });

  it("drops it on cancel without sending anything", () => {
    const onSubmit = vi.fn();
    renderWithProviders(<Host enabled onSubmit={onSubmit} />);
    choose();

    act(() => screen.getByRole("button", { name: /cancel/i }).click());

    expect(onSubmit).not.toHaveBeenCalled();
    expect(screen.getByTestId("staged")).toHaveTextContent("none");
  });

  it("drops it when the position moves underneath it", () => {
    // A staged move is a claim about a position. Once the opponent has
    // played, the path may not even be legal — and submitting it would be
    // rejected at best. `sequence` is the authoritative ply, so a change is
    // the server saying the board is not what the player was looking at.
    const onSubmit = vi.fn();
    renderWithProviders(<Host enabled onSubmit={onSubmit} />);
    choose();
    expect(screen.getByTestId("staged")).toHaveTextContent("staged");

    act(() => screen.getByRole("button", { name: "opponent moves" }).click());

    expect(screen.getByTestId("staged")).toHaveTextContent("none");
    expect(onSubmit).not.toHaveBeenCalled();
  });
});

describe("the pending control", () => {
  it("puts play first for a keyboard and second on screen", () => {
    // The two orders are deliberately different, and `flex-row-reverse` is
    // the only thing making them so — which means a refactor that drops it
    // silently changes the reading order. This is what notices.
    renderWithProviders(
      <PendingMove onConfirm={() => {}} onCancel={() => {}} disabled={false} />,
    );

    const buttons = screen.getAllByRole("button");
    expect(buttons[0]).toHaveAccessibleName(/play move/i);
    expect(buttons[1]).toHaveAccessibleName(/cancel/i);
  });

  it("cannot be confirmed while a move is already in flight", () => {
    render(<div />); // keeps the previous tree out of this query
    renderWithProviders(<PendingMove onConfirm={() => {}} onCancel={() => {}} disabled />);

    expect(screen.getByRole("button", { name: /play move/i })).toBeDisabled();
    // Cancel stays live: a player whose move is in flight must still be able
    // to clear the one they staged behind it.
    expect(screen.getByRole("button", { name: /cancel/i })).toBeEnabled();
  });
});
