// UI layout + navigation state (workspace tab, activity view, panels).
import { create } from "zustand";

export type ActivityView = "explorer" | "search" | "scm";
/** The agent column shows the conversation, agent settings, or provider setup. */
export type AgentView = "chat" | "settings" | "providers";
export type BottomTab = "terminal" | "output" | "problems" | "tests";
export type InsightsView =
  | "map"
  | "logs"
  | "missions"
  | "checkpoints"
  | "approvals"
  | "repository";
/** The Inspector's two halves: the pre-push scan, and the app it probes. */
export type InspectorView = "scan" | "review" | "live";
/** What the Workspace tab is showing about the selected workspace. */
export type WorkbenchView =
  | "documents"
  | "tasks"
  | "uploads"
  | "sources"
  | "changes";

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
  agentView: AgentView;
  setAgentView: (v: AgentView) => void;

  insightsView: InsightsView;
  setInsightsView: (v: InsightsView) => void;

  inspectorView: InspectorView;
  setInspectorView: (v: InspectorView) => void;

  workbenchView: WorkbenchView;
  setWorkbenchView: (v: WorkbenchView) => void;
  /**
   * The workspace being worked in, or null for the list.
   *
   * In the store rather than local state for the same reason as the sub-view
   * enums above: the menu bar and status bar can then deep-link into one.
   */
  activeWorkspaceId: string | null;
  setActiveWorkspaceId: (id: string | null) => void;
  /** The document open in the workspace viewer, relative to its folder. */
  activeArtifactPath: string | null;
  setActiveArtifactPath: (path: string | null) => void;

  /** Path of the diff most recently opened, for the agent's context chips. */
  lastDiffPath: string | null;
  setLastDiffPath: (path: string | null) => void;

  /**
   * The conversation this tab is attached to, or null for "the latest one".
   *
   * Per tab rather than persisted: two windows on one project can sit in
   * different conversations, which is the point of having sessions at all.
   */
  sessionTarget: string | null;
  setSessionTarget: (id: string | null) => void;

  /**
   * Bumped when something asks the search panel to take focus (Edit ▸ Find in
   * files). A counter rather than a boolean, so two requests in a row both land.
   */
  searchFocusNonce: number;
  focusSearch: () => void;
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
  agentView: "chat",
  setAgentView: (agentView) => set({ agentView }),

  insightsView: "map",
  setInsightsView: (insightsView) => set({ insightsView }),

  inspectorView: "scan",
  setInspectorView: (inspectorView) => set({ inspectorView }),

  workbenchView: "documents",
  setWorkbenchView: (workbenchView) => set({ workbenchView }),
  activeWorkspaceId: null,
  // Opening a different workspace must not leave the previous one's document
  // on screen.
  setActiveWorkspaceId: (activeWorkspaceId) =>
    set((s) =>
      s.activeWorkspaceId === activeWorkspaceId
        ? { activeWorkspaceId }
        : { activeWorkspaceId, activeArtifactPath: null },
    ),
  activeArtifactPath: null,
  setActiveArtifactPath: (activeArtifactPath) => set({ activeArtifactPath }),

  lastDiffPath: null,
  setLastDiffPath: (lastDiffPath) => set({ lastDiffPath }),

  sessionTarget: null,
  setSessionTarget: (sessionTarget) => set({ sessionTarget }),

  searchFocusNonce: 0,
  focusSearch: () => set((s) => ({ searchFocusNonce: s.searchFocusNonce + 1 })),
}));
