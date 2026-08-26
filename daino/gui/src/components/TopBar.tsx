import { useEffect } from "react";
import { useWorkspace } from "../api/hooks";
import { useUIStore } from "../store/uiStore";
import { useAgentStore } from "../store/agentStore";
import { WORKSPACE_TABS } from "../tabs/registry";
import { BRAND } from "../lib/branding";

export function TopBar() {
  const { data: workspace } = useWorkspace();
  const activeTab = useUIStore((s) => s.activeWorkspaceTab);
  const setActiveTab = useUIStore((s) => s.setActiveWorkspaceTab);
  const toggleAgent = useUIStore((s) => s.toggleAgent);
  const agentVisible = useUIStore((s) => s.agentVisible);

  const selectedModel = useAgentStore((s) => s.selectedModel);
  const setModel = useAgentStore((s) => s.setModel);

  const models = workspace?.models ?? [];

  // default the model picker to the first available model
  useEffect(() => {
    if (!selectedModel && models.length) setModel(models[0]);
  }, [models, selectedModel, setModel]);

  return (
    <div className="topbar">
      <div className="wordmark">
        <span className="dot" />
        {BRAND}
      </div>
      {workspace && (
        <div className="project-name" title={workspace.root}>
          {workspace.name}
        </div>
      )}

      <div className="tabs">
        {WORKSPACE_TABS.map((t) => (
          <button
            key={t.id}
            className={`tab ${activeTab === t.id ? "active" : ""}`}
            onClick={() => setActiveTab(t.id)}
            title={t.hint}
          >
            {t.label}
          </button>
        ))}
      </div>

      <select
        className="model-picker"
        value={selectedModel ?? ""}
        onChange={(e) => setModel(e.target.value || null)}
        title="Model"
      >
        {models.length === 0 && <option value="">default</option>}
        {models.map((m) => (
          <option key={m} value={m}>
            {m}
          </option>
        ))}
      </select>

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
        className="btn subtle"
        onClick={toggleAgent}
        title={agentVisible ? "Collapse the agent panel" : "Expand the agent panel"}
      >
        {agentVisible ? "Agent ›" : "‹ Agent"}
      </button>
    </div>
  );
}
