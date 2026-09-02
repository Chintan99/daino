import type { LiveEvent } from "../../store/agentStore";

function str(v: unknown, fallback = ""): string {
  return typeof v === "string" ? v : fallback;
}
function num(v: unknown): number {
  return typeof v === "number" ? v : 0;
}

// Maps a single live event to a friendly card. Never renders raw JSON.
export function ToolEventCard({ item }: { item: LiveEvent }) {
  const e = item.event;
  switch (item.kind) {
    case "ToolStarted":
      return (
        <div className="tool-card run">
          <div className="head">
            <span className="mark">●</span>
            <span className="tool">{str(e.tool, "tool")}</span>
          </div>
          {str(e.summary) && <div className="detail">{str(e.summary)}</div>}
        </div>
      );
    case "ToolCompleted":
      return (
        <div className="tool-card ok">
          <div className="head">
            <span className="mark">✓</span>
            <span className="tool">{str(e.tool, "tool")}</span>
            {num(e.duration_seconds) > 0 && (
              <span className="detail">
                {num(e.duration_seconds).toFixed(1)}s
              </span>
            )}
          </div>
          {str(e.summary) && <div className="detail">{str(e.summary)}</div>}
        </div>
      );
    case "ToolFailed":
      return (
        <div className="tool-card fail">
          <div className="head">
            <span className="mark">✗</span>
            <span className="tool">{str(e.tool, "tool")}</span>
          </div>
          {str(e.error) && <div className="detail">{str(e.error)}</div>}
        </div>
      );
    case "FileChanged": {
      const added = num(e.added);
      const removed = num(e.removed);
      return (
        <div className="tool-card ok">
          <div className="head">
            <span className="mark">✎</span>
            <span className="tool">{str(e.path, "file")}</span>
            <span className="detail">{str(e.action, "changed")}</span>
          </div>
          {(added > 0 || removed > 0) && (
            <div className="detail diffstat">
              <span className="add">+{added}</span>{" "}
              <span className="del">-{removed}</span>
            </div>
          )}
        </div>
      );
    }
    case "TestsStarted":
      return (
        <div className="tool-card run">
          <div className="head">
            <span className="mark">●</span>
            <span className="tool">Running tests…</span>
          </div>
        </div>
      );
    case "TestsCompleted": {
      const passed = Boolean(e.passed);
      return (
        <div className={`tool-card ${passed ? "ok" : "fail"}`}>
          <div className="head">
            <span className="mark">{passed ? "✓" : "✗"}</span>
            <span className="tool">Tests</span>
            <span className="detail">
              {num(e.passed_count)} passed · {num(e.failed_count)} failed
            </span>
          </div>
        </div>
      );
    }
    // The plan itself is the panel's job (TodoPanel); repeating it in the
    // stream on every update buried the work between five copies of the same
    // checklist. What belongs here is the transition.
    case "TodoUpdated":
      return null;
    case "TodoCompleted":
      return (
        <div className="task-line done">
          <span className="mark">✓</span>
          <span className="label">{str(e.content)}</span>
        </div>
      );
    case "TodoFailed":
      return (
        <div className="task-line failed">
          <span className="mark">✗</span>
          <span className="label">{str(e.content)}</span>
        </div>
      );
    case "error":
      return (
        <div className="tool-card fail">
          <div className="head">
            <span className="mark">✗</span>
            <span className="tool">Error</span>
          </div>
          <div className="detail">{str(e.message, "Unknown error")}</div>
        </div>
      );
    case "ModelSelected":
      return (
        <div className="tool-card">
          <div className="detail">Model: {str(e.model ?? e.name)}</div>
        </div>
      );
    case "TaskSplit":
      return (
        <div className="tool-card">
          <div className="detail">
            Task split into {(e.slices as unknown[] | undefined)?.length ?? 0} smaller
            tasks: {str(e.title ?? e.name)}
          </div>
          <div className="detail">{str(e.reason)}</div>
        </div>
      );
    case "TaskStarted":
    case "TaskCompleted":
    case "MissionStarted":
    case "MissionCompleted":
      return (
        <div className="tool-card">
          <div className="detail">
            {item.kind.replace(/([A-Z])/g, " $1").trim()}
            {str(e.title ?? e.name) ? `: ${str(e.title ?? e.name)}` : ""}
          </div>
        </div>
      );
    default:
      return null; // ignore noisy/low-signal events in the chat view
  }
}
