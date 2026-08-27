import { useState } from "react";
import { useAgentConfig } from "../../api/hooks";
import { useAgentStore } from "../../store/agentStore";
import { useUIStore } from "../../store/uiStore";
import { SessionSection } from "./settings/SessionSection";
import { RolesSection } from "./settings/RolesSection";
import { InstructionsSection } from "./settings/InstructionsSection";
import { MemorySection } from "./settings/MemorySection";
import { PlaybooksSection } from "./settings/PlaybooksSection";

/**
 * Agent customization, in the agent column.
 *
 * One section at a time rather than a card grid: this column is a third of the
 * window, and a grid squeezed into it reads worse than a list. Every section is
 * backed by a service the terminal client already drives through a slash
 * command, which is also why there are no cards here for skills, hooks, plugins,
 * or MCP servers — D[Ai]NO does not implement them, and an inert card promising
 * one is worse than not offering it.
 */
type SectionId = "session" | "roles" | "instructions" | "memory" | "playbooks";

const SECTIONS: {
  id: SectionId;
  label: string;
  hint: string;
  command: string;
}[] = [
  {
    id: "session",
    label: "Autonomy & effort",
    hint: "How much this conversation may do on its own",
    command: "/mode · /effort · /verbose",
  },
  {
    id: "roles",
    label: "Agent roles",
    hint: "Which model plans, builds, reviews, debugs",
    command: "routing",
  },
  {
    id: "instructions",
    label: "Instructions",
    hint: "Always-on DAINO.md guidance, in layers",
    command: "DAINO.md",
  },
  {
    id: "memory",
    label: "Memory",
    hint: "Facts, decisions, and failures worth keeping",
    command: "/memory",
  },
  {
    id: "playbooks",
    label: "Playbooks",
    hint: "Reusable staged procedures",
    command: "/playbooks",
  },
];

export function AgentSettingsPanel() {
  const setAgentView = useUIStore((s) => s.setAgentView);
  const sessionId = useAgentStore((s) => s.sessionId);
  const { data: config, isLoading, error } = useAgentConfig(sessionId);
  const [section, setSection] = useState<SectionId | null>(null);

  const active = SECTIONS.find((item) => item.id === section) ?? null;

  return (
    <div className="provider-panel">
      <div className="panel-header">
        <button
          className="btn icon"
          title={active ? "All settings" : "Back to the conversation"}
          onClick={() => (active ? setSection(null) : setAgentView("chat"))}
        >
          ‹
        </button>
        {active ? active.label : "Agent settings"}
        <span className="spacer" />
        {active && <span className="muted mono">{active.command}</span>}
      </div>

      <div className="panel-body">
        {isLoading && <div className="empty">Loading…</div>}
        {error && (
          <div className="empty">
            Could not load the agent configuration:{" "}
            {error instanceof Error ? error.message : String(error)}
          </div>
        )}

        {config && sessionId && !active && (
          <div className="cfg-list">
            {SECTIONS.map((item) => (
              <button
                key={item.id}
                className="cfg-row"
                onClick={() => setSection(item.id)}
              >
                <span className="grow">
                  <span className="name">{item.label}</span>
                  <span className="detail">{item.hint}</span>
                </span>
                <span className="value mono">{summary(item.id, config)}</span>
                <span className="arrow">›</span>
              </button>
            ))}
            <button className="cfg-row" onClick={() => setAgentView("providers")}>
              <span className="grow">
                <span className="name">Providers</span>
                <span className="detail">Connect, edit, and test a model provider</span>
              </span>
              <span className="arrow">›</span>
            </button>
          </div>
        )}

        {config && sessionId && active?.id === "session" && (
          <SessionSection config={config} sessionId={sessionId} />
        )}
        {config && active?.id === "roles" && <RolesSection config={config} />}
        {config && sessionId && active?.id === "instructions" && (
          <InstructionsSection config={config} sessionId={sessionId} />
        )}
        {config && sessionId && active?.id === "memory" && (
          <MemorySection config={config} sessionId={sessionId} />
        )}
        {config && active?.id === "playbooks" && <PlaybooksSection config={config} />}
      </div>
    </div>
  );
}

/** The one value worth showing on a collapsed row. */
function summary(id: SectionId, config: NonNullable<ReturnType<typeof useAgentConfig>["data"]>) {
  switch (id) {
    case "session":
      return config.autonomy.mode;
    case "roles": {
      const distinct = new Set(config.roles.map((role) => role.profile).filter(Boolean));
      return distinct.size === 1 ? [...distinct][0] : `${distinct.size} models`;
    }
    case "instructions": {
      const present = config.instructions.files.filter((file) => file.exists).length;
      return present ? `${present} file${present === 1 ? "" : "s"}` : "none";
    }
    case "memory":
      return config.memory.total ? String(config.memory.total) : "empty";
    case "playbooks":
      return String(config.playbooks.length);
    default:
      return "";
  }
}
