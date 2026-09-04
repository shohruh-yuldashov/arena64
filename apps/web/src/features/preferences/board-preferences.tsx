import { useEffect } from "react";

import { usePreferences } from "@/features/profile/model/queries";

/**
 * Applies `board_theme`, `piece_set` and `show_coordinates` to the
 * document — A64-025.5B §22, A64-025.6D §28.
 *
 * ## Why this exists at all
 *
 * Both fields have been on `PreferencesResponse` since A64-012.5 and were
 * read by **nothing**. A player picked "Wood", the form saved it, the
 * server stored it, and every board in the product stayed exactly as it
 * was. This is the component that makes the setting true.
 *
 * ## One attribute on the root, not a prop through four components
 *
 * The board is drawn in the game room, in the lobby's preview, and wherever
 * one is drawn next. Threading two strings to each of them is three places
 * to forget; a data attribute on `<html>` is one place, and CSS does the
 * rest — the palettes live in `globals.css` beside the tokens they
 * override, which is also where anybody looking for them would look.
 *
 * It follows `shared/theme`, which sets the `dark` class on the same
 * element for the same reason.
 *
 * ## Mounted only when signed in
 *
 * `/profile/preferences` is an account read: an anonymous visitor issuing
 * it gets a 401 and nothing else. The shell mounts this behind the same
 * check that guards the account menu, so the request is never made without
 * a session to make it with.
 *
 * Renders nothing. It is an effect with a query attached, and a `<div>`
 * would be a layout participant with no reason to be one.
 */
export function BoardPreferences() {
  const preferences = usePreferences();
  const boardTheme = preferences.data?.gameplay.board_theme;
  const pieceSet = preferences.data?.gameplay.piece_set;
  // A64-025.6D §28. The third of the five write-only preferences, and the
  // second to be closed. It rides the same attribute mechanism because the
  // board that honours it is drawn on two routes and neither should have
  // to ask for the preference itself.
  const coordinates = preferences.data?.gameplay.show_coordinates;

  useEffect(() => {
    const root = document.documentElement;
    if (boardTheme !== undefined) root.dataset.boardTheme = boardTheme;
    if (pieceSet !== undefined) root.dataset.pieceSet = pieceSet;
    if (coordinates !== undefined) root.dataset.coordinates = coordinates ? "on" : "off";

    // Cleared on unmount, which is what signing out does. Leaving the last
    // player's board on the document would show the next person who signs
    // in on this browser a theme they never chose.
    return () => {
      delete root.dataset.boardTheme;
      delete root.dataset.pieceSet;
      delete root.dataset.coordinates;
    };
  }, [boardTheme, pieceSet, coordinates]);

  return null;
}
