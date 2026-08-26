// Open terminal tabs.
import { create } from "zustand";

interface TerminalState {
  ids: string[];
  activeId: string | null;
  addTerminal: (id: string) => void;
  removeTerminal: (id: string) => void;
  setActive: (id: string) => void;
}

export const useTerminalStore = create<TerminalState>((set) => ({
  ids: [],
  activeId: null,
  addTerminal: (id) =>
    set((s) =>
      s.ids.includes(id)
        ? { activeId: id }
        : { ids: [...s.ids, id], activeId: id },
    ),
  removeTerminal: (id) =>
    set((s) => {
      const ids = s.ids.filter((x) => x !== id);
      const activeId =
        s.activeId === id ? (ids.length ? ids[ids.length - 1] : null) : s.activeId;
      return { ids, activeId };
    }),
  setActive: (id) => set({ activeId: id }),
}));
