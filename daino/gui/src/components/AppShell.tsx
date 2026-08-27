import { Panel, PanelGroup, PanelResizeHandle } from "react-resizable-panels";
import { useUIStore } from "../store/uiStore";
import { getTab } from "../tabs/registry";
import { useSessionSocket } from "../ws/useSessionSocket";
import { useShortcuts } from "../lib/useShortcuts";
import { useWakeLock } from "../lib/useWakeLock";
import { useTabAttention } from "../lib/useTabAttention";
import { useSettings } from "../api/hooks";
import { TopBar } from "./TopBar";
import { StatusBar } from "./StatusBar";
import { ActivityBar } from "./ActivityBar";
import { AgentPanel } from "./agent/AgentPanel";
import { AgentRail } from "./agent/AgentRail";
import { BottomPanel } from "./bottom/BottomPanel";
import { ExplorerPanel } from "./explorer/ExplorerPanel";
import { SearchPanel } from "./search/SearchPanel";
import { SourceControlPanel } from "./scm/SourceControlPanel";
import { Dialogs } from "./ui/Dialogs";

function ActivitySidebar() {
  const view = useUIStore((s) => s.activityView);
  if (view === "search") return <SearchPanel />;
  if (view === "scm") return <SourceControlPanel />;
  return <ExplorerPanel />;
}

export function AppShell() {
  // one shared session websocket for the whole app
  // Follows the conversation this tab selected; "latest" on first open.
  useSessionSocket(useUIStore((s) => s.sessionTarget) ?? "latest");
  // ⌘B sidebar, ⌘J panel, ⌘I agent, and the rest of the menu's bindings.
  useShortcuts();
  // One switch governs both halves of "do not sleep while working": the server
  // inhibits system sleep, the browser keeps this display awake.
  const { data: projectSettings } = useSettings();
  useWakeLock(projectSettings?.keep_awake !== false);
  useTabAttention();

  const activeTabId = useUIStore((s) => s.activeWorkspaceTab);
  const sidebarCollapsed = useUIStore((s) => s.sidebarCollapsed);
  const bottomVisible = useUIStore((s) => s.bottomVisible);
  const agentVisible = useUIStore((s) => s.agentVisible);

  const tab = getTab(activeTabId);
  const Workspace = tab.component;
  const showSidebar = tab.showSidebar && !sidebarCollapsed;
  const showBottom = tab.showBottomPanel && bottomVisible;

  // Remount the group when the structural layout changes so
  // react-resizable-panels always sees a stable set of children.
  const layoutKey = `${activeTabId}-${showSidebar ? "s" : ""}-${
    agentVisible ? "a" : ""
  }`;

  return (
    <div className="app-shell">
      <TopBar />
      <div className="app-body">
        {tab.showSidebar && <ActivityBar />}
        <PanelGroup
          key={layoutKey}
          direction="horizontal"
          style={{ flex: 1, minWidth: 0 }}
        >
          {showSidebar && (
            <>
              <Panel defaultSize={18} minSize={12} maxSize={40}>
                <ActivitySidebar />
              </Panel>
              <PanelResizeHandle className="rp-handle" />
            </>
          )}

          <Panel defaultSize={agentVisible ? 54 : 82} minSize={30}>
            {tab.showBottomPanel ? (
              <PanelGroup
                key={showBottom ? "with-bottom" : "no-bottom"}
                direction="vertical"
              >
                <Panel defaultSize={showBottom ? 66 : 100} minSize={20}>
                  <Workspace />
                </Panel>
                {showBottom && (
                  <>
                    <PanelResizeHandle className="rp-handle" />
                    <Panel defaultSize={34} minSize={12}>
                      <BottomPanel />
                    </Panel>
                  </>
                )}
              </PanelGroup>
            ) : (
              <Workspace />
            )}
          </Panel>

          {agentVisible && (
            <>
              <PanelResizeHandle className="rp-handle" />
              <Panel defaultSize={28} minSize={18} maxSize={48}>
                <AgentPanel />
              </Panel>
            </>
          )}
        </PanelGroup>
        {!agentVisible && <AgentRail />}
      </div>
      <StatusBar />
      {/* Prompts and reference sheets the menu opens. */}
      <Dialogs />
    </div>
  );
}
