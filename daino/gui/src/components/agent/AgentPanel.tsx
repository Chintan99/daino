import { useEffect, useMemo, useRef, useState } from "react";
import { useSessionMessages } from "../../api/hooks";
import { useAgentStore } from "../../store/agentStore";
import { useUIStore } from "../../store/uiStore";
import { sendChatMessage } from "../../lib/agent";
import { AgentMessage } from "./AgentMessage";
import { ToolEventCard } from "./ToolEventCard";
import { ApprovalCard } from "./ApprovalCard";
import { ContextBar } from "./ContextBar";
import { BRAND } from "../../lib/branding";

// Cap rendered transcript items so very long sessions stay responsive.
const MESSAGE_CAP = 200;

export function AgentPanel() {
  const sessionId = useAgentStore((s) => s.sessionId);
  const wsStatus = useAgentStore((s) => s.wsStatus);
  const turnRunning = useAgentStore((s) => s.turnRunning);
  const pendingUser = useAgentStore((s) => s.pendingUser);
  const thinking = useAgentStore((s) => s.thinking);
  const streaming = useAgentStore((s) => s.streaming);
  const events = useAgentStore((s) => s.events);
  const liveStart = useAgentStore((s) => s.liveStart);
  const approvals = useAgentStore((s) => s.approvals);

  const toggleAgent = useUIStore((s) => s.toggleAgent);

  const { data } = useSessionMessages(sessionId);
  const [input, setInput] = useState("");
  const streamRef = useRef<HTMLDivElement | null>(null);

  const messages = useMemo(() => {
    const all = data?.messages ?? [];
    return all.length > MESSAGE_CAP ? all.slice(all.length - MESSAGE_CAP) : all;
  }, [data]);

  // live turn events (only those since the current turn started)
  const liveEvents = turnRunning ? events.slice(liveStart) : [];

  // autoscroll to bottom as content grows
  useEffect(() => {
    const el = streamRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [messages, liveEvents.length, thinking, streaming, pendingUser, approvals]);

  const submit = () => {
    if (sendChatMessage(input)) setInput("");
  };

  return (
    <div className="agent-panel">
      <div className="panel-header">
        <span className={`dot-status dot-${wsStatus}`} />
        {BRAND} Agent
        <span className="spacer" />
        {turnRunning && (
          <button
            className="btn subtle sm"
            onClick={() => useAgentStore.getState().send?.({ type: "cancel" })}
            title="Stop the running turn"
          >
            Stop
          </button>
        )}
        <button
          className="btn icon"
          title="Collapse the agent panel"
          onClick={toggleAgent}
        >
          ›
        </button>
      </div>

      <div className="agent-stream" ref={streamRef}>
        {messages.length === 0 && !pendingUser && (
          <div className="empty">
            Ask {BRAND} to build, refactor, explain, or run your project.
          </div>
        )}

        {messages.map((m) => (
          <AgentMessage key={m.id} message={m} />
        ))}

        {/* optimistic user message for the running turn */}
        {pendingUser && (
          <div className="msg user">
            <div className="role">You</div>
            <div className="md" style={{ whiteSpace: "pre-wrap" }}>
              {pendingUser}
            </div>
          </div>
        )}

        {/* live tool / file / test / todo cards */}
        {liveEvents.map((item) => (
          <ToolEventCard key={item.id} item={item} />
        ))}

        {/* ephemeral reasoning + streamed answer */}
        {turnRunning && thinking && (
          <div className="thinking">{thinking}</div>
        )}
        {turnRunning && streaming && (
          <div className="streaming">{streaming}</div>
        )}
        {turnRunning && !thinking && !streaming && liveEvents.length === 0 && (
          <div className="thinking">{BRAND} is working…</div>
        )}

        {/* approvals always shown */}
        {approvals.map((a) => (
          <ApprovalCard key={a.id} approval={a} />
        ))}
      </div>

      <div className="agent-composer">
        <ContextBar />
        <div className="composer-row">
          <textarea
            className="composer-input"
            placeholder={
              turnRunning
                ? `${BRAND} is working…`
                : `Message ${BRAND}…  (Enter to send)`
            }
            value={input}
            disabled={wsStatus !== "open"}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                submit();
              }
            }}
          />
          <button
            className="btn primary"
            disabled={wsStatus !== "open" || turnRunning || !input.trim()}
            onClick={submit}
          >
            Send
          </button>
        </div>
      </div>
    </div>
  );
}
