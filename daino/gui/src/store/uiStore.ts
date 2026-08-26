// UI layout + navigation state (workspace tab, activity view, bottom panel).
import { create } from "zustand";

export type ActivityView = "explorer" | "search" | "scm";
export type BottomTab = "terminal" | "output" | "problems" | "tests" | "gitdiff";

interface UIState {
  activeWorkspaceTab: string; // id from the tab registry
  setActiveWorkspaceTab: (id: string) => void;

  activityView: ActivityView;
  setActivityView: (v: ActivityView) => void;
  sidebarCollapsed: boolean;
  toggleSidebar: () => void;

  bottomVisible: boolean;
  setBottomVisible: (v: boolean) => void;
  bottomTab: BottomTab;
  setBottomTab: (t: BottomTab) => void;

  agentVisible: boolean;
  toggleAgent: () => void;

  gitDiffPath: string | null;
  gitDiffStaged: boolean;
  openGitDiff: (path: string, staged: boolean) => void;
}

export const useUIStore = create<UIState>((set) => ({
  activeWorkspaceTab: "code",
  setActiveWorkspaceTab: (id) => set({ activeWorkspaceTab: id }),

  activityView: "explorer",
  setActivityView: (v) => set({ activityView: v }),
  sidebarCollapsed: false,
  toggleSidebar: () =>
    set((s) => ({ sidebarCollapsed: !s.sidebarCollapsed })),

  bottomVisible: true,
  setBottomVisible: (v) => set({ bottomVisible: v }),
  bottomTab: "terminal",
  setBottomTab: (t) => set({ bottomTab: t, bottomVisible: true }),

  agentVisible: true,
  toggleAgent: () => set((s) => ({ agentVisible: !s.agentVisible })),

  gitDiffPath: null,
  gitDiffStaged: false,
  openGitDiff: (path, staged) =>
    set({
      gitDiffPath: path,
      gitDiffStaged: staged,
      bottomTab: "gitdiff",
      bottomVisible: true,
    }),
}));
