// A rename that has been computed but not yet written.
//
// The gap between those two is the whole point: a cross-file rename is the most
// consequential single keystroke an editor offers, and it should not happen
// before someone has seen how far it reaches.
import { create } from "zustand";
import type { TextEdit } from "../api/types";

export interface PendingRename {
  symbol: string;
  newName: string;
  edits: Record<string, TextEdit[]>;
  files: number;
  count: number;
}

interface RenameState {
  pending: PendingRename | null;
  show: (rename: PendingRename) => void;
  clear: () => void;
}

export const useRenameStore = create<RenameState>((set) => ({
  pending: null,
  show: (pending) => set({ pending }),
  clear: () => set({ pending: null }),
}));
