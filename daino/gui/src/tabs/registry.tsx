// Workspace tab registry. Adding a new top-level workspace is a matter of
// registering an entry here — no layout refactor needed.
import type { ComponentType } from "react";
import { EditorWorkspace } from "../components/editor/EditorWorkspace";
import { DesignWorkspace } from "../components/design/DesignWorkspace";
import { WorkbenchWorkspace } from "../components/workbench/WorkbenchWorkspace";
import { InspectorMark } from "../components/inspector/InspectorMark";
import { InspectorWorkspace } from "../components/inspector/InspectorWorkspace";
import { InsightsWorkspace } from "../components/insights/InsightsWorkspace";

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
   * verdict, work in flight — declares it here rather than the chrome
   * special-casing tab ids.
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
