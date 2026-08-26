// Design workspace selection state.
import { create } from "zustand";

interface DesignState {
  activeDesignId: string | null;
  selectedNodeIds: string[];
  /** Node whose source is open in the inspector's editor, if any. */
  sourceNodeId: string | null;
  /** Node open in the full-screen viewer, if any. */
  viewerNodeId: string | null;
  setActiveDesign: (id: string | null) => void;
  setSelectedNodes: (ids: string[]) => void;
  setSourceNode: (id: string | null) => void;
  setViewerNode: (id: string | null) => void;
}

export const useDesignStore = create<DesignState>((set) => ({
  activeDesignId: null,
  selectedNodeIds: [],
  sourceNodeId: null,
  viewerNodeId: null,
  setActiveDesign: (id) =>
    set({
      activeDesignId: id,
      selectedNodeIds: [],
      sourceNodeId: null,
      viewerNodeId: null,
    }),
  setSelectedNodes: (ids) => set({ selectedNodeIds: ids }),
  setSourceNode: (sourceNodeId) =>
    set((s) => ({
      sourceNodeId,
      // Opening the editor implies selecting what it edits.
      selectedNodeIds: sourceNodeId ? [sourceNodeId] : s.selectedNodeIds,
    })),
  setViewerNode: (viewerNodeId) =>
    set((s) => ({
      viewerNodeId,
      selectedNodeIds: viewerNodeId ? [viewerNodeId] : s.selectedNodeIds,
    })),
}));
