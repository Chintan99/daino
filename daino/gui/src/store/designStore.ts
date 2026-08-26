// Design workspace selection state.
import { create } from "zustand";

interface DesignState {
  activeDesignId: string | null;
  selectedNodeIds: string[];
  setActiveDesign: (id: string | null) => void;
  setSelectedNodes: (ids: string[]) => void;
}

export const useDesignStore = create<DesignState>((set) => ({
  activeDesignId: null,
  selectedNodeIds: [],
  setActiveDesign: (id) =>
    set({ activeDesignId: id, selectedNodeIds: [] }),
  setSelectedNodes: (ids) => set({ selectedNodeIds: ids }),
}));
