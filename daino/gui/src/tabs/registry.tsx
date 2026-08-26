// Workspace tab registry. Adding a new tab (e.g. a future "PLAN" tab) is a matter
// of registering an entry here — no layout refactor needed.
import type { ComponentType } from "react";
import { EditorWorkspace } from "../components/editor/EditorWorkspace";
import { DesignWorkspace } from "../components/design/DesignWorkspace";
import { PreviewWorkspace } from "../components/preview/PreviewWorkspace";

export interface WorkspaceTab {
  id: string;
  label: string;
  component: ComponentType;
  /** whether the left sidebar / bottom panel are shown for this tab */
  showSidebar: boolean;
  showBottomPanel: boolean;
}

export const WORKSPACE_TABS: WorkspaceTab[] = [
  {
    id: "code",
    label: "CODE",
    component: EditorWorkspace,
    showSidebar: true,
    showBottomPanel: true,
  },
  {
    id: "design",
    label: "DESIGN",
    component: DesignWorkspace,
    showSidebar: false,
    showBottomPanel: false,
  },
  {
    id: "preview",
    label: "PREVIEW",
    component: PreviewWorkspace,
    showSidebar: false,
    showBottomPanel: false,
  },
  // Future: { id: "plan", label: "PLAN", component: PlanWorkspace, ... }
];

export function getTab(id: string): WorkspaceTab {
  return WORKSPACE_TABS.find((t) => t.id === id) ?? WORKSPACE_TABS[0];
}
