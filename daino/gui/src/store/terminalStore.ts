// Open terminal tabs.
import { create } from "zustand";

interface TerminalState {
  ids: string[];
  activeId: string | null;
  /** Why the last attempt to open a shell failed, for the panel to show. */
  error: string | null;
  addTerminal: (id: string) => void;
  removeTerminal: (id: string) => void;
  setActive: (id: string) => void;
  setError: (message: string | null) => void;
}

export const useTerminalStore = create<TerminalState>((set) => ({
  ids: [],
  activeId: null,
  error: null,
  addTerminal: (id) =>
    set((s) =>
      s.ids.includes(id)
        ? { activeId: id, error: null }
        : { ids: [...s.ids, id], activeId: id, error: null },
    ),
  removeTerminal: (id) =>
    set((s) => {
      const ids = s.ids.filter((x) => x !== id);
      const activeId =
        s.activeId === id ? (ids.length ? ids[ids.length - 1] : null) : s.activeId;
      return { ids, activeId };
    }),
  setActive: (id) => set({ activeId: id }),
  setError: (error) => set({ error }),
}));
