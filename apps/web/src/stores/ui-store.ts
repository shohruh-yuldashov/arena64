import { create } from "zustand";

interface UiState {
  isMobileNavOpen: boolean;
  openMobileNav: () => void;
  closeMobileNav: () => void;
  toggleMobileNav: () => void;
}

/**
 * Client/UI state — architecture.md AD-22's third state category, kept
 * disjoint from TanStack Query's server state (`providers/query-provider.tsx`)
 * and next-intl's locale state. Zustand rather than `useState` here
 * because this state is read and written by siblings — the header's menu
 * button and the nav panel it opens — that don't share a parent close
 * enough for prop drilling to be reasonable.
 *
 * No persistence middleware: whether the mobile nav is open must not
 * survive a reload, so nothing here should ever write to storage.
 */
export const useUiStore = create<UiState>((set) => ({
  isMobileNavOpen: false,
  openMobileNav: () => {
    set({ isMobileNavOpen: true });
  },
  closeMobileNav: () => {
    set({ isMobileNavOpen: false });
  },
  toggleMobileNav: () => {
    set((state) => ({ isMobileNavOpen: !state.isMobileNavOpen }));
  },
}));
