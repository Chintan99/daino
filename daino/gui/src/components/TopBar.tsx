import { useQALatest, useWorkspace } from "../api/hooks";
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
  const { data: workspace } = useWorkspace();
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
        {workspace && (
          <div className="project-name" title={workspace.root}>
            {workspace.name}
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
              {t.id === "inspector" && <InspectorMark />}
            </button>
          ))}
        </div>
      </div>
    </>
  );
}

/**
 * A dot on the INSPECTOR tab: pulsing while a scan runs, then coloured by the
 * verdict it landed on. The verdict outlives the notification, so the tab keeps
 * showing whether this checkout is currently cleared to push.
 */
function InspectorMark() {
  const { data: qa } = useQALatest();
  if (qa?.running) return <span className="tab-mark running" title="Inspection running" />;
  const verdict = qa?.report?.verdict;
  if (!verdict || verdict === "unknown") return null;
  return (
    <span
      className={`tab-mark v-${verdict}`}
      title={
        verdict === "pass"
          ? "Last inspection: safe to push"
          : verdict === "warn"
            ? "Last inspection: review before pushing"
            : "Last inspection: do not push"
      }
    />
  );
}
