// Open terminal tabs.
import { create } from "zustand";

interface TerminalState {
  ids: string[];
  activeId: string | null;
  /** Why the last attempt to open a shell failed, for the panel to show. */
  error: string | null;
  /**
   * Text waiting for a terminal's socket to open.
   *
   * Running a task creates the terminal and wants to send it a command, but the
   * socket does not exist until the panel has mounted an xterm for it. Queueing
   * is what makes "Run" work on the first click rather than the second.
   */
  pending: Record<string, string[]>;
  addTerminal: (id: string) => void;
  removeTerminal: (id: string) => void;
  setActive: (id: string) => void;
  setError: (message: string | null) => void;
  queueInput: (id: string, data: string) => void;
  /** Take everything queued for a terminal, clearing it. */
  drainInput: (id: string) => string[];
}

export const useTerminalStore = create<TerminalState>((set, get) => ({
  ids: [],
  activeId: null,
  error: null,
  pending: {},
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
  queueInput: (id, data) =>
    set((s) => ({ pending: { ...s.pending, [id]: [...(s.pending[id] ?? []), data] } })),
  drainInput: (id) => {
    const queued = get().pending[id] ?? [];
    if (queued.length) {
      set((s) => {
        const pending = { ...s.pending };
        delete pending[id];
        return { pending };
      });
    }
    return queued;
  },
}));
