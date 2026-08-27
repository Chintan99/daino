import { useEffect, useMemo, useRef, useState, type DragEvent } from "react";
import { useSessionMessages } from "../../api/hooks";
import { useAgentStore } from "../../store/agentStore";
import { useUIStore } from "../../store/uiStore";
import { useSettingsStore } from "../../store/settingsStore";
import { sendChatMessage } from "../../lib/agent";
import { AgentMessage } from "./AgentMessage";
import { ToolEventCard } from "./ToolEventCard";
import { ApprovalCard } from "./ApprovalCard";
import { ContextBar } from "./ContextBar";
import { ComposerControls } from "./ComposerControls";
import { attachFiles, MAX_ATTACHMENTS } from "../../lib/attach";
import { ActivityRunner } from "./ActivityRunner";
import { SessionBar } from "./SessionBar";
import { ModelBar } from "./ModelBar";
import { TodoPanel } from "./TodoPanel";
import { LiveChangeset } from "./LiveChangeset";
import { ProviderPanel } from "./ProviderPanel";
import { AgentSettingsPanel } from "./AgentSettingsPanel";
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
  const agentView = useUIStore((s) => s.agentView);
  const showThinking = useSettingsStore((s) => s.showThinking);

  const { data } = useSessionMessages(sessionId);
  const [input, setInput] = useState("");
  const [dragging, setDragging] = useState(false);
  const [attachError, setAttachError] = useState("");
  const streamRef = useRef<HTMLDivElement | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);

  /**
   * Take files from a drop, a paste, or the picker.
   *
   * They are stored in the project and attached as paths, which is what the
   * agent can act on; images included, though no configured model can look at
   * one yet (see lib/attach.ts).
   */
  const takeFiles = async (files: readonly File[]) => {
    if (!files.length) return;
    const result = await attachFiles(files);
    setAttachError(result.errors.join(" "));
  };

  const onDrop = (event: DragEvent<HTMLDivElement>) => {
    event.preventDefault();
    setDragging(false);
    void takeFiles([...event.dataTransfer.files]);
  };

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

  // Settings take over the column; the conversation is one click away.
  if (agentView === "providers") {
    return (
      <div className="agent-panel">
        <ProviderPanel />
      </div>
    );
  }
  if (agentView === "settings") {
    return (
      <div className="agent-panel">
        <AgentSettingsPanel />
      </div>
    );
  }

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

      <SessionBar />
      <ActivityRunner />
      <ModelBar />
      <TodoPanel />

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
        {turnRunning && thinking && showThinking && (
          <div className="thinking">{thinking}</div>
        )}
        {turnRunning && streaming && (
          <div className="streaming">{streaming}</div>
        )}
        {turnRunning && !(thinking && showThinking) && !streaming && liveEvents.length === 0 && (
          <div className="thinking">{BRAND} is working…</div>
        )}

        {/* What the turn has edited so far, and what it is on now. */}
        <LiveChangeset />

        {/* approvals always shown */}
        {approvals.map((a) => (
          <ApprovalCard key={a.id} approval={a} />
        ))}
      </div>

      <div
        className={`agent-composer ${dragging ? "dropping" : ""}`}
        onDragOver={(e) => {
          if (!e.dataTransfer.types.includes("Files")) return;
          e.preventDefault();
          setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={onDrop}
      >
        <ComposerControls />
        <ContextBar />
        {attachError && (
          <div className="composer-note bad">
            {attachError}
            <button className="btn subtle sm" onClick={() => setAttachError("")}>
              Dismiss
            </button>
          </div>
        )}
        <div className="composer-row">
          <textarea
            className="composer-input"
            placeholder={
              turnRunning
                ? `${BRAND} is working…`
                : `Message ${BRAND}…  (Enter to send, drop or paste files)`
            }
            value={input}
            disabled={wsStatus !== "open"}
            onChange={(e) => setInput(e.target.value)}
            onPaste={(e) => {
              const files = [...e.clipboardData.files];
              if (!files.length) return;
              // A pasted screenshot is a file, not text; keep it out of the box.
              e.preventDefault();
              void takeFiles(files);
            }}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                submit();
              }
            }}
          />
          <div className="composer-actions">
            <button
              className="btn icon"
              title={`Attach files or images (up to ${MAX_ATTACHMENTS})`}
              aria-label="Attach files"
              onClick={() => fileInputRef.current?.click()}
            >
              <svg
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                strokeWidth={1.6}
                strokeLinecap="round"
                strokeLinejoin="round"
                aria-hidden="true"
              >
                <path d="M20.5 11.5 12 20a5 5 0 0 1-7-7l8-8a3.5 3.5 0 0 1 5 5l-8 8a2 2 0 0 1-3-3l7.5-7.5" />
              </svg>
            </button>
            <input
              ref={fileInputRef}
              type="file"
              multiple
              hidden
              onChange={(e) => {
                void takeFiles([...(e.target.files ?? [])]);
                e.target.value = "";
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
        {dragging && <div className="drop-hint">Drop to attach</div>}
      </div>
    </div>
  );
}
