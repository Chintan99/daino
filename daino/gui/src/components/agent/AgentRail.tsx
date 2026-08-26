import { useUIStore } from "../../store/uiStore";
import { useAgentStore } from "../../store/agentStore";
import { BRAND } from "../../lib/branding";

/**
 * The collapsed form of the agent column.
 *
 * Collapsing it to a labelled rail rather than removing it keeps the agent
 * discoverable — and keeps a running turn visible — when the canvas or editor
 * needs the width.
 */
export function AgentRail() {
  const toggleAgent = useUIStore((s) => s.toggleAgent);
  const wsStatus = useAgentStore((s) => s.wsStatus);
  const turnRunning = useAgentStore((s) => s.turnRunning);
  const approvals = useAgentStore((s) => s.approvals);

  return (
    <div className="agent-rail">
      <button
        className="btn icon"
        title="Expand the agent panel"
        onClick={toggleAgent}
      >
        ‹
      </button>
      <span
        className={`pulse ${turnRunning ? "live" : "idle"}`}
        title={turnRunning ? `${BRAND} is working` : `${BRAND} · ${wsStatus}`}
      />
      <div
        className="vertical-label"
        onClick={toggleAgent}
        style={{ cursor: "pointer" }}
        title="Expand the agent panel"
      >
        {BRAND} Agent
      </div>
      {approvals.length > 0 && (
        <span className="badge warn" title="Approval waiting">
          {approvals.length}
        </span>
      )}
    </div>
  );
}
