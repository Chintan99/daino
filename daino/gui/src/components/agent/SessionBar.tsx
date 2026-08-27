import { useEffect, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { api } from "../../api/client";
import { qk, useSessions } from "../../api/hooks";
import { useAgentStore } from "../../store/agentStore";
import { useUIStore } from "../../store/uiStore";

/**
 * Which conversation this tab is in, and how to leave it.
 *
 * Every project used to open the *latest* session forever, so the browser had
 * one endless chat: two days of history went into every prompt, and a fresh
 * request was answered in the context of an old one. Sessions already existed
 * (the terminal client's `/new`); this is the browser's way to use them.
 */
function relative(iso: string): string {
  const at = new Date(iso).getTime();
  if (!Number.isFinite(at)) return "";
  const minutes = Math.round((Date.now() - at) / 60_000);
  if (minutes < 1) return "now";
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  return `${Math.round(hours / 24)}d ago`;
}

export function SessionBar() {
  const qc = useQueryClient();
  const { data } = useSessions();
  const sessionId = useAgentStore((s) => s.sessionId);
  const turnRunning = useAgentStore((s) => s.turnRunning);
  const setTarget = useUIStore((s) => s.setSessionTarget);
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const hostRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (!hostRef.current?.contains(e.target as Node)) setOpen(false);
    };
    window.addEventListener("mousedown", onDown);
    return () => window.removeEventListener("mousedown", onDown);
  }, [open]);

  const sessions = data?.sessions ?? [];
  const current = sessions.find((item) => item.id === sessionId);

  const switchTo = (id: string) => {
    setOpen(false);
    if (id === sessionId) return;
    setTarget(id);
  };

  const startNew = async () => {
    setBusy(true);
    try {
      const created = await api.createSession("");
      await qc.invalidateQueries({ queryKey: qk.sessions });
      setOpen(false);
      setTarget(created.id);
    } catch (err) {
      window.alert(
        `Could not start a session: ${err instanceof Error ? err.message : String(err)}`,
      );
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="session-bar" ref={hostRef}>
      <button
        className="session-current"
        title={
          current
            ? `${current.title}\n${current.message_count} messages · ${relative(current.updated_at)}`
            : "Conversation"
        }
        onClick={() => setOpen(!open)}
      >
        <span className="name">{current?.title ?? "Conversation"}</span>
        {current && current.message_count > 0 && (
          <span className="count">{current.message_count}</span>
        )}
        <span className="chev">⌄</span>
      </button>
      <button
        className="btn sm"
        disabled={busy || turnRunning}
        title={
          turnRunning
            ? "Finish or stop the running turn first"
            : "Start a fresh conversation — no history from this one"
        }
        onClick={() => void startNew()}
      >
        New
      </button>

      {open && (
        <div className="menu top session-menu" role="menu">
          <div className="menu-label">Conversations</div>
          {sessions.length === 0 && <div className="combo-empty">No conversations yet.</div>}
          {sessions.map((item) => (
            <button
              key={item.id}
              className={`menu-item ${item.id === sessionId ? "checked" : ""}`}
              onClick={() => switchTo(item.id)}
            >
              <span className="tick">{item.id === sessionId ? "✓" : ""}</span>
              <span className="grow">
                {item.title}
                <span className="hint">
                  {item.message_count} message{item.message_count === 1 ? "" : "s"} ·{" "}
                  {relative(item.updated_at)}
                </span>
              </span>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
