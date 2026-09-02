// The command palette's open state.
//
// In a store rather than local state because three different things open it —
// a keybinding, the menu bar, and the tasks button — and each wants to preset a
// different mode prefix.
import { create } from "zustand";

interface QuickOpenState {
  open: boolean;
  /** Preset text, e.g. "@" to land straight in symbol mode. */
  initialQuery: string;
  show: (initialQuery?: string) => void;
  close: () => void;
}

export const useQuickOpenStore = create<QuickOpenState>((set) => ({
  open: false,
  initialQuery: "",
  show: (initialQuery = "") => set({ open: true, initialQuery }),
  close: () => set({ open: false, initialQuery: "" }),
}));
