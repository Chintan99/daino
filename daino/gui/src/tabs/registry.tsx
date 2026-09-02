// Workspace tab registry. Adding a new top-level workspace is a matter of
// registering an entry here — no layout refactor needed.
import { lazy, type ComponentType } from "react";
import { EditorWorkspace } from "../components/editor/EditorWorkspace";
import { WorkspaceMark } from "../components/workbench/WorkspaceMark";
import { InspectorMark } from "../components/inspector/InspectorMark";

// CODE is eager: it is what opens, so deferring it would only add a spinner in
// front of the first thing anyone sees. Every other workspace is loaded when
// its tab is first opened — DESIGN alone pulls in ReactFlow, which is not
// something a session spent editing files should pay for.
//
// The tab marks stay eager. They render inside the tab bar from the moment the
// app starts, so lazily loading a 12-pixel dot would be all cost.
const DesignWorkspace = lazy(() =>
  import("../components/design/DesignWorkspace").then((m) => ({
    default: m.DesignWorkspace,
  })),
);
const WorkbenchWorkspace = lazy(() =>
  import("../components/workbench/WorkbenchWorkspace").then((m) => ({
    default: m.WorkbenchWorkspace,
  })),
);
const InspectorWorkspace = lazy(() =>
  import("../components/inspector/InspectorWorkspace").then((m) => ({
    default: m.InspectorWorkspace,
  })),
);
const InsightsWorkspace = lazy(() =>
  import("../components/insights/InsightsWorkspace").then((m) => ({
    default: m.InsightsWorkspace,
  })),
);

export interface WorkspaceTab {
  id: string;
  label: string;
  hint: string;
  component: ComponentType;
  /** whether the left sidebar / bottom panel are shown for this tab */
  showSidebar: boolean;
  showBottomPanel: boolean;
  /**
   * A small indicator rendered inside the tab button.
   *
   * A tab that carries a result worth seeing from anywhere — the Inspector's
   * verdict, a maturity label, work in flight — declares it here rather than
   * the chrome special-casing tab ids.
   */
  mark?: ComponentType;
}

export const WORKSPACE_TABS: WorkspaceTab[] = [
  {
    id: "code",
    label: "CODE",
    hint: "Edit files, review diffs, run terminals",
    component: EditorWorkspace,
    showSidebar: true,
    showBottomPanel: true,
  },
  {
    id: "design",
    label: "DESIGN",
    hint: "A blank canvas for HTML, mock-ups, and architecture",
    component: DesignWorkspace,
    showSidebar: false,
    showBottomPanel: false,
  },
  {
    id: "workspace",
    label: "WORKSPACE",
    hint: "Documents, research, planning, and analysis — the work that is not code",
    component: WorkbenchWorkspace,
    showSidebar: false,
    showBottomPanel: false,
    mark: WorkspaceMark,
  },
  {
    id: "inspector",
    label: "INSPECTOR",
    hint: "Pre-push QA and vulnerability assessment, and the running app it probes",
    component: InspectorWorkspace,
    showSidebar: false,
    showBottomPanel: false,
    mark: InspectorMark,
  },
  {
    id: "insights",
    label: "INSIGHTS",
    hint: "Execution map, logs, missions, checkpoints, and approvals",
    component: InsightsWorkspace,
    showSidebar: false,
    showBottomPanel: false,
  },
];

export function getTab(id: string): WorkspaceTab {
  return WORKSPACE_TABS.find((t) => t.id === id) ?? WORKSPACE_TABS[0];
}
