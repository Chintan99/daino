import { useAgentStore } from "../../store/agentStore";
import { useSettingsStore } from "../../store/settingsStore";

function str(v: unknown, f = ""): string {
  return typeof v === "string" ? v : f;
}

/** One line of an event's own fields, for kinds without a dedicated renderer. */
function summarize(ev: Record<string, unknown>): string {
  return Object.entries(ev)
    .filter(([key]) => key !== "kind")
    .map(([key, value]) => `${key}=${typeof value === "object" ? "…" : String(value)}`)
    .join(" ")
    .slice(0, 240);
}

/** The events worth reading during a normal run. */
const SUMMARY_KINDS = [
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
];

/** Streamed text is shown in the transcript; repeating it here is noise. */
const STREAM_KINDS = ["ModelReasoningChunk", "ModelStreamChunk"];

// A running log derived from agent tool/file/test events. Settings ▸ Diagnostics
// ▸ Verbose event stream widens it to every event the backend published, which
// is what you want when the question is "why did nothing happen?".
export function OutputPanel() {
  const events = useAgentStore((s) => s.events);
  const verbose = useSettingsStore((s) => s.verboseEvents);

  const lines = events
    .filter((e) =>
      verbose ? !STREAM_KINDS.includes(e.kind) : SUMMARY_KINDS.includes(e.kind),
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
        // Verbose mode reaches kinds without a renderer; show the fields
        // rather than the bare kind, which on its own explains nothing.
        return verbose ? `${kind} ${summarize(ev)}` : kind;
    }
  };

  return (
    <div className="scroll-y" style={{ height: "100%", padding: "6px 0" }}>
      {lines.length === 0 && (
        <div className="empty">
          {verbose ? "No events yet." : "No output yet."}
        </div>
      )}
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
