import { useAgentStore } from "../../store/agentStore";

function str(v: unknown, f = ""): string {
  return typeof v === "string" ? v : f;
}

// A running log derived from agent tool/file/test events.
export function OutputPanel() {
  const events = useAgentStore((s) => s.events);

  const lines = events
    .filter((e) =>
      [
        "ToolStarted",
        "ToolCompleted",
        "ToolFailed",
        "FileChanged",
        "TestsStarted",
        "TestsCompleted",
        "ModelSelected",
        "MissionStarted",
        "MissionCompleted",
        "error",
      ].includes(e.kind),
    )
    .slice(-300);

  const render = (kind: string, ev: Record<string, unknown>): string => {
    switch (kind) {
      case "ToolStarted":
        return `● ${str(ev.tool)} ${str(ev.summary)}`;
      case "ToolCompleted":
        return `✓ ${str(ev.tool)} ${str(ev.summary)}`;
      case "ToolFailed":
        return `✗ ${str(ev.tool)} ${str(ev.error)}`;
      case "FileChanged":
        return `✎ ${str(ev.action)} ${str(ev.path)}`;
      case "TestsStarted":
        return `● tests started`;
      case "TestsCompleted":
        return `✓ tests: ${String(ev.passed_count ?? 0)} passed, ${String(
          ev.failed_count ?? 0,
        )} failed`;
      case "ModelSelected":
        return `model: ${str(ev.model ?? ev.name)}`;
      case "error":
        return `✗ ${str(ev.message)}`;
      default:
        return kind;
    }
  };

  return (
    <div className="scroll-y" style={{ height: "100%", padding: "6px 0" }}>
      {lines.length === 0 && <div className="empty">No output yet.</div>}
      {lines.map((e) => (
        <div key={e.id} className="log-line">
          <span className="muted">
            {new Date(e.at).toLocaleTimeString()}{" "}
          </span>
          {render(e.kind, e.event)}
        </div>
      ))}
    </div>
  );
}
