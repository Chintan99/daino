// Workspace tab registry. Adding a new top-level workspace is a matter of
// registering an entry here — no layout refactor needed.
import type { ComponentType } from "react";
import { EditorWorkspace } from "../components/editor/EditorWorkspace";
import { DesignWorkspace } from "../components/design/DesignWorkspace";
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
    id: "inspector",
    label: "INSPECTOR",
    hint: "Pre-push QA and vulnerability assessment, and the running app it probes",
    component: InspectorWorkspace,
    showSidebar: false,
    showBottomPanel: false,
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
