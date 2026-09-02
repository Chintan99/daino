import { useEffect, useMemo, useRef, useState, type DragEvent } from "react";
import { qk, useSessionMessages, useQueryClient } from "../../api/hooks";
import { api } from "../../api/client";
import { useAgentStore } from "../../store/agentStore";
import { useUIStore } from "../../store/uiStore";
import { useSettingsStore } from "../../store/settingsStore";
import { sendChatMessage } from "../../lib/agent";
import {
  SLASH_COMMANDS,
  runGuiSlashCommand,
  type SlashCommand,
} from "../../lib/slashCommands";
import { SlashMenu } from "./SlashMenu";
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
import { RunStatusBar } from "./RunStatusBar";
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
  const stopping = useAgentStore((s) => s.stopping);
  const pendingUser = useAgentStore((s) => s.pendingUser);
  const thinking = useAgentStore((s) => s.thinking);
  const streaming = useAgentStore((s) => s.streaming);
  const events = useAgentStore((s) => s.events);
  const liveStart = useAgentStore((s) => s.liveStart);
  const approvals = useAgentStore((s) => s.approvals);

  const toggleAgent = useUIStore((s) => s.toggleAgent);
  const agentView = useUIStore((s) => s.agentView);
  const showThinking = useSettingsStore((s) => s.showThinking);

  const setSessionTarget = useUIStore((s) => s.setSessionTarget);
  const qc = useQueryClient();

  const { data } = useSessionMessages(sessionId);
  const [input, setInput] = useState("");
  const [dragging, setDragging] = useState(false);
  const [attachError, setAttachError] = useState("");
  const [notice, setNotice] = useState("");
  // Slash-command dropdown: which item is highlighted, and whether it was
  // dismissed (Escape) for the current query.
  const [slashIndex, setSlashIndex] = useState(0);
  const [slashClosed, setSlashClosed] = useState(false);
  const streamRef = useRef<HTMLDivElement | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);

  // Command name still being typed (no space yet) → suggest commands.
  const commandMatches = useMemo<SlashCommand[]>(() => {
    if (!input.startsWith("/") || input.includes(" ")) return [];
    const q = input.toLowerCase();
    return SLASH_COMMANDS.filter((c) => c.name.startsWith(q));
  }, [input]);

  // "/cmd <partial>" where the command takes enum values → suggest those values.
  const argCommand = useMemo<SlashCommand | null>(() => {
    if (!input.startsWith("/") || !input.includes(" ")) return null;
    const name = "/" + input.slice(1).split(/\s+/)[0].toLowerCase();
    const cmd = SLASH_COMMANDS.find((c) => c.name === name);
    return cmd?.options ? cmd : null;
  }, [input]);
  const argValue = argCommand
    ? input.slice(input.indexOf(" ") + 1).trim().toLowerCase()
    : "";

  const menuItems = useMemo<SlashCommand[]>(() => {
    if (argCommand)
      return argCommand
        .options!.filter((o) => o.startsWith(argValue))
        .map((o) => ({ name: o, description: "" }));
    return commandMatches;
  }, [argCommand, argValue, commandMatches]);
  const slashOpen = !slashClosed && menuItems.length > 0;

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

  const startNew = async () => {
    try {
      const created = await api.createSession("");
      await qc.invalidateQueries({ queryKey: qk.sessions });
      setSessionTarget(created.id);
    } catch (err) {
      setAttachError(
        `Could not start a session: ${err instanceof Error ? err.message : String(err)}`,
      );
    }
  };

  // Apply a value-taking command (/effort, /mode, /verbose) to the session.
  const applyValue = async (cmd: SlashCommand, value: string) => {
    setInput("");
    setSlashClosed(true);
    setSlashIndex(0);
    if (!sessionId) return;
    try {
      let label = value;
      if (cmd.name === "/effort") {
        await api.setEffort(sessionId, value);
        label = `Reasoning effort → ${value}`;
      } else if (cmd.name === "/mode") {
        await api.setAutonomy(sessionId, value);
        label = `Autonomy → ${value}`;
      } else if (cmd.name === "/verbose") {
        await api.setVerbose(sessionId, value === "on");
        label = `Verbose progress ${value === "on" ? "on" : "off"}`;
      }
      await qc.invalidateQueries({ queryKey: qk.agentConfig(sessionId) });
      setNotice(label);
      window.setTimeout(() => setNotice(""), 2500);
    } catch (err) {
      setAttachError(
        `Could not apply ${cmd.name} ${value}: ${err instanceof Error ? err.message : String(err)}`,
      );
    }
  };

  // Carry out a slash command: value commands apply, browser-native ones act
  // here, the rest go to the agent as an instruction (e.g. /build, /test).
  const runCommand = (raw: string) => {
    const parts = raw.trim().split(/\s+/);
    const name = parts[0].toLowerCase();
    const value = parts.slice(1).join(" ").toLowerCase();
    const cmd = SLASH_COMMANDS.find((c) => c.name === name);
    if (cmd?.options && value) {
      void applyValue(cmd, value);
      return;
    }
    if (name === "/new") {
      void startNew();
      setInput("");
      return;
    }
    if (runGuiSlashCommand(raw)) {
      setInput("");
      return;
    }
    if (sendChatMessage(raw)) setInput("");
  };

  const submit = () => {
    const text = input.trim();
    if (text.startsWith("/")) {
      runCommand(text);
      return;
    }
    if (sendChatMessage(input)) setInput("");
  };

  // An item chosen from the dropdown — either a command or, in value mode, a
  // value for the active command.
  const onPickItem = (item: SlashCommand) => {
    setSlashIndex(0);
    if (argCommand) {
      void applyValue(argCommand, item.name);
      return;
    }
    if (item.options) {
      // Step into value selection: show the command's values next.
      setSlashClosed(false);
      setInput(item.name + " ");
      return;
    }
    // A required argument (<...>) waits to be typed; the rest run.
    if (item.usage?.startsWith("<")) {
      setSlashClosed(true);
      setInput(item.name + " ");
    } else {
      runCommand(item.name);
    }
  };

  const onComposerChange = (value: string) => {
    setInput(value);
    setSlashClosed(false);
    setSlashIndex(0);
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
            disabled={stopping}
            onClick={() => useAgentStore.getState().requestStop()}
            title="Stop the running turn"
          >
            {stopping ? "Stopping…" : "Stop"}
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
      <RunStatusBar />
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
        {notice && <div className="composer-note">✓ {notice}</div>}
        <div className="composer-row">
          {slashOpen && (
            <SlashMenu
              items={menuItems}
              index={slashIndex}
              onPick={onPickItem}
              onHover={setSlashIndex}
            />
          )}
          <textarea
            className="composer-input"
            placeholder={
              turnRunning
                ? `${BRAND} is working…  (type / for commands)`
                : `Message ${BRAND}…  (Enter to send, / for commands)`
            }
            value={input}
            disabled={wsStatus !== "open"}
            onChange={(e) => onComposerChange(e.target.value)}
            onPaste={(e) => {
              const files = [...e.clipboardData.files];
              if (!files.length) return;
              // A pasted screenshot is a file, not text; keep it out of the box.
              e.preventDefault();
              void takeFiles(files);
            }}
            onKeyDown={(e) => {
              // While the command dropdown is open it owns the arrows and Enter.
              if (slashOpen) {
                if (e.key === "ArrowDown") {
                  e.preventDefault();
                  setSlashIndex((i) => (i + 1) % menuItems.length);
                  return;
                }
                if (e.key === "ArrowUp") {
                  e.preventDefault();
                  setSlashIndex((i) => (i - 1 + menuItems.length) % menuItems.length);
                  return;
                }
                if (e.key === "Escape") {
                  e.preventDefault();
                  setSlashClosed(true);
                  return;
                }
                if (e.key === "Enter" || e.key === "Tab") {
                  e.preventDefault();
                  onPickItem(menuItems[slashIndex]);
                  return;
                }
              }
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
