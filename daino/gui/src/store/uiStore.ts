// UI layout + navigation state (workspace tab, activity view, panels).
import { create } from "zustand";

export type ActivityView = "explorer" | "search" | "scm";
export type BottomTab = "terminal" | "output" | "problems" | "tests";
export type InsightsView =
  | "map"
  | "logs"
  | "qa"
  | "missions"
  | "checkpoints"
  | "approvals"
  | "repository";

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

  /** The agent column collapses to a labelled rail rather than vanishing. */
  agentVisible: boolean;
  toggleAgent: () => void;
  setAgentVisible: (v: boolean) => void;

  insightsView: InsightsView;
  setInsightsView: (v: InsightsView) => void;

  /** Path of the diff most recently opened, for the agent's context chips. */
  lastDiffPath: string | null;
  setLastDiffPath: (path: string | null) => void;
}

export const useUIStore = create<UIState>((set) => ({
  activeWorkspaceTab: "code",
  setActiveWorkspaceTab: (id) => set({ activeWorkspaceTab: id }),

  activityView: "explorer",
  setActivityView: (v) => set({ activityView: v }),
  sidebarCollapsed: false,
  toggleSidebar: () => set((s) => ({ sidebarCollapsed: !s.sidebarCollapsed })),

  bottomVisible: true,
  setBottomVisible: (v) => set({ bottomVisible: v }),
  bottomTab: "terminal",
  setBottomTab: (t) => set({ bottomTab: t, bottomVisible: true }),

  agentVisible: true,
  toggleAgent: () => set((s) => ({ agentVisible: !s.agentVisible })),
  setAgentVisible: (agentVisible) => set({ agentVisible }),

  insightsView: "map",
  setInsightsView: (insightsView) => set({ insightsView }),

  lastDiffPath: null,
  setLastDiffPath: (lastDiffPath) => set({ lastDiffPath }),
}));
