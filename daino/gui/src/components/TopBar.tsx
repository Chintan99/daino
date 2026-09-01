import { useProjectInfo } from "../api/hooks";
import { useUIStore } from "../store/uiStore";
import { WORKSPACE_TABS } from "../tabs/registry";
import { BRAND } from "../lib/branding";
import { MenuBar } from "./ui/MenuBar";
import { RuntimeToggle } from "./RuntimeToggle";
import { useAppMenus } from "./menus/useAppMenus";

/**
 * The application chrome: a menu bar over the workspace tab row.
 *
 * They are two rows rather than one because the menu is not a workspace: a
 * single row made every new menu compete for width with CODE / DESIGN /
 * INSPECTOR / INSIGHTS, and the tabs are the navigation users reach for most.
 */
export function TopBar() {
  const { data: project } = useProjectInfo();
  const menus = useAppMenus();
  const activeTab = useUIStore((s) => s.activeWorkspaceTab);
  const setActiveTab = useUIStore((s) => s.setActiveWorkspaceTab);
  const toggleAgent = useUIStore((s) => s.toggleAgent);
  const agentVisible = useUIStore((s) => s.agentVisible);

  return (
    <>
      <div className="menubar">
        <div className="wordmark">
          <span className="dot" />
          {BRAND}
        </div>
        <MenuBar menus={menus} />
        <span className="spacer" />
        {project && (
          <div className="project-name" title={project.root}>
            {project.name}
          </div>
        )}
        <RuntimeToggle />
        <a
          className="btn icon"
          href="/docs"
          target="_blank"
          rel="noreferrer noopener"
          title={`${BRAND} documentation — how to configure, route models, and run it`}
        >
          ?
        </a>
        <button
          className="btn subtle sm"
          onClick={toggleAgent}
          title={agentVisible ? "Collapse the agent panel" : "Expand the agent panel"}
        >
          {agentVisible ? "Agent ›" : "‹ Agent"}
        </button>
      </div>

      <div className="topbar">
        <div className="tabs">
          {WORKSPACE_TABS.map((t) => (
            <button
              key={t.id}
              className={`tab ${activeTab === t.id ? "active" : ""}`}
              onClick={() => setActiveTab(t.id)}
              title={t.hint}
            >
              {t.label}
              {t.mark && <t.mark />}
            </button>
          ))}
        </div>
      </div>
    </>
  );
}
