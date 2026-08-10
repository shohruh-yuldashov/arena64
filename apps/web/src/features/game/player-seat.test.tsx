import { screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { LOW_TIME_SECONDS, PlayerSeat } from "@/features/game/ui/player-seat";
import { renderWithProviders } from "@/shared/test/render";

/**
 * The seat, the clock and the turn — A64-025.6 §28.
 *
 * `game.test.tsx` covers the protocol and the board; this covers the three
 * presentation contracts A64-025.6 introduced, each of which is a way the
 * screen can lie to a player: about whose move it is, about how much time
 * is left, and about who they are playing.
 */

const IDENTITY = {
  id: "019fb9ea-0a0c-7cec-9c5f-402727c31a96",
  username: "rival",
  display_name: "Rival",
  avatar_url: null,
  thumbnail_url: null,
};

function seat(overrides: Partial<Parameters<typeof PlayerSeat>[0]> = {}) {
  return renderWithProviders(
    <PlayerSeat
      side="light"
      identity={IDENTITY}
      rating={null}
      ms={60_000}
      active={false}
      awaiting={false}
      isViewer={false}
      running
      {...overrides}
    />,
  );
}

describe("the player seat", () => {
  it("names the player from the authoritative identity", () => {
    seat();
    expect(screen.getByText("Rival")).toBeVisible();
  });

  it("falls back to a neutral label rather than an empty seat", () => {
    // The identity read does not retry during a game. A blank name beside a
    // clock reads as a bug; a word reads as a slow request.
    seat({ identity: undefined });
    expect(screen.getByText(/unknown player|noma'lum|неизвестный/i)).toBeVisible();
  });

  it("says whose move it is in words, not only in colour", () => {
    // WCAG 1.4.1. The tint and the brand digits are reinforcement; this is
    // the signal somebody who cannot tell them apart reads.
    const { unmount } = seat({ active: true });
    expect(screen.getByText(/on the move|yurmoqda|ход идёт/i)).toBeVisible();
    unmount();

    seat({ active: false });
    expect(screen.queryByText(/on the move|yurmoqda|ход идёт/i)).not.toBeInTheDocument();
  });

  it("warns in words when the active clock is nearly out", () => {
    seat({ active: true, ms: LOW_TIME_SECONDS * 1000 - 1 });
    expect(screen.getByText(/low on time|vaqt tugayapti|мало времени/i)).toBeVisible();
  });

  it("does not warn about a clock that is not running", () => {
    // A finished game freezes both clocks. Shouting about the loser's two
    // remaining seconds after the result is in is noise about nothing.
    seat({ active: true, ms: 1_000, running: false });
    expect(
      screen.queryByText(/low on time|vaqt tugayapti|мало времени/i),
    ).not.toBeInTheDocument();
  });

  it("does not warn about the opponent's clock through this seat", () => {
    // Only the side to move is losing time. An inactive seat at two seconds
    // is a player who is about to get their time back.
    seat({ active: false, ms: 1_000 });
    expect(
      screen.queryByText(/low on time|vaqt tugayapti|мало времени/i),
    ).not.toBeInTheDocument();
  });

  it("gives the clock an accessible name carrying the side", () => {
    // The digits change four times a second and are deliberately not a live
    // region; a reader asks for them, so they must be findable.
    seat({ side: "dark" });
    expect(screen.getByLabelText(/dark|qora|чёрны/i)).toHaveTextContent("1:00");
  });

  it("marks the viewer's own seat", () => {
    const { container } = seat({ isViewer: true });
    expect(within(container).getByText(/· you|· siz|· вы/i)).toBeVisible();
  });

  it("rounds the seat rating the server sent as a float", () => {
    // A64-025.6B. The server sends the stored value precisely so this
    // decision is the client's; a seat showing `1487.5` would be exposing
    // Glicko-2 internals to a player mid-game.
    const { container } = seat({ rating: { value: 1487.5, is_provisional: false } });
    expect(within(container).getByText("1488")).toBeVisible();
  });

  it("qualifies a provisional rating in words, not only with a mark", () => {
    // The "?" is shorthand a sighted player learns; "question mark" is not
    // what it means, so the word is what a screen reader gets.
    const { container } = seat({ rating: { value: 1200, is_provisional: true } });
    expect(
      within(container).getByText(/provisional|dastlabki|предварительный/i),
    ).toBeInTheDocument();
  });

  it("shows nothing at all when the match carries no rating", () => {
    // Every match created before ratings existed has none. A placeholder
    // would read as a load that never finishes.
    const { container } = seat({ rating: null });
    expect(container.textContent).not.toMatch(/\d{4}/);
    expect(
      within(container).queryByText(/provisional|dastlabki|предварительный/i),
    ).not.toBeInTheDocument();
  });
});
