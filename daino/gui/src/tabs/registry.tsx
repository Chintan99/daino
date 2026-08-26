// Workspace tab registry. Adding a new top-level workspace is a matter of
// registering an entry here — no layout refactor needed.
import type { ComponentType } from "react";
import { EditorWorkspace } from "../components/editor/EditorWorkspace";
import { DesignWorkspace } from "../components/design/DesignWorkspace";
import { PreviewWorkspace } from "../components/preview/PreviewWorkspace";
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
    id: "preview",
    label: "PREVIEW",
    hint: "Run the project and watch it live",
    component: PreviewWorkspace,
    showSidebar: false,
    showBottomPanel: false,
  },
  {
    id: "insights",
    label: "INSIGHTS",
    hint: "Execution map, logs, QA, missions, and approvals",
    component: InsightsWorkspace,
    showSidebar: false,
    showBottomPanel: false,
  },
];

export function getTab(id: string): WorkspaceTab {
  return WORKSPACE_TABS.find((t) => t.id === id) ?? WORKSPACE_TABS[0];
}
