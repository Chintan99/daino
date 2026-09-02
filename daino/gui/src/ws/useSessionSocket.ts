// Single shared session WebSocket. Mounted once in AppShell.
// Feeds live events into the agent store and invalidates queries on turn_complete
// and DesignUpdated so React Query re-fetches persisted state.
import { useEffect, useRef } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { qk } from "../api/hooks";
import { useAgentStore } from "../store/agentStore";
import { useDesignStore } from "../store/designStore";
import { useUIStore } from "../store/uiStore";
import type {
  ClientSessionMessage,
  ServerSessionMessage,
  WsEvent,
} from "../api/types";
import { roleActivity, toolActivity } from "../lib/activity";
import { wsUrl } from "./url";

function num(v: unknown): number {
  return typeof v === "number" ? v : 0;
}
function str(v: unknown): string {
  return typeof v === "string" ? v : "";
}

/**
 * The TUI's activity mapping (daino/tui/screens/workspace.py), applied to the
 * live event stream. Kept beside the socket because this is the only place the
 * raw events arrive.
 */
function applyActivity(
  kind: string,
  event: WsEvent,
  s: ReturnType<typeof useAgentStore.getState>,
): void {
  switch (kind) {
    case "MissionCreated":
      s.setActivity("planning", "shaping the task");
      return;
    case "AgentRoleChanged":
      s.setActivity(roleActivity(str(event.role)), `${str(event.role)} active`);
      return;
    case "ModelReasoningChunk":
      if (!s.thinking) s.setActivity("thinking", "model reasoning");
      return;
    case "MissionStarted":
      s.setActivity("building", "mission running");
      return;
    case "TaskStarted":
      s.setActivity("building", str(event.title));
      return;
    case "ToolStarted":
      s.setActivity(toolActivity(str(event.tool)), str(event.summary));
      return;
    case "ToolFailed":
      s.setActivity("failed", str(event.error).slice(0, 60));
      return;
    case "FileChanged":
      s.setActivity("building", str(event.path));
      return;
    case "TestsStarted":
      s.setActivity("verifying", "running checks");
      return;
    case "TestsCompleted": {
      const passed = Boolean(event.passed);
      const total = num(event.passed_count) + num(event.failed_count);
      s.setActivity(
        passed ? "verifying" : "failed",
        `tests ${num(event.passed_count)}/${total}`,
      );
      return;
    }
    case "MissionCompleted":
      s.setActivity("completed", "all work verified");
      return;
    case "MissionFailed":
      s.setActivity("failed", "needs attention");
      return;
    default:
      return;
  }
}

export function useSessionSocket(target: string = "latest") {
  const qc = useQueryClient();
  const wsRef = useRef<WebSocket | null>(null);
  const sessionIdRef = useRef<string | null>(null);
  const pingRef = useRef<number | null>(null);

  useEffect(() => {
    // A different conversation means none of the previous one's live state
    // applies: events, plan, file list and chips all belong to the session
    // being left.
    useAgentStore.getState().resetForSession();
    let closedByUs = false;
    let retry = 0;
    let reconnectTimer: number | null = null;

    const store = useAgentStore.getState;

    const connect = () => {
      store().setWsStatus("connecting");
      const ws = new WebSocket(wsUrl(`/ws/session/${target}`));
      wsRef.current = ws;

      ws.onopen = () => {
        retry = 0;
        store().setWsStatus("open");
        const send = (msg: ClientSessionMessage) => {
          if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(msg));
        };
        store().setSend(send);
        // keepalive
        pingRef.current = window.setInterval(() => {
          if (ws.readyState === WebSocket.OPEN)
            ws.send(JSON.stringify({ type: "ping" }));
        }, 25000);
      };

      ws.onmessage = (ev) => {
        let msg: ServerSessionMessage;
        try {
          msg = JSON.parse(ev.data as string);
        } catch {
          return;
        }
        handle(msg);
      };

      ws.onerror = () => store().setWsStatus("error");

      ws.onclose = () => {
        store().setWsStatus("closed");
        store().setSend(null);
        if (pingRef.current) {
          window.clearInterval(pingRef.current);
          pingRef.current = null;
        }
        if (!closedByUs) {
          retry += 1;
          const delay = Math.min(1000 * retry, 8000);
          reconnectTimer = window.setTimeout(connect, delay);
        }
      };
    };

    const handle = (msg: ServerSessionMessage) => {
      const s = store();
      switch (msg.type) {
        case "session": {
          sessionIdRef.current = msg.session_id;
          s.setSession(msg.session_id);
          // A refresh reconnects mid-turn: pick the running state back up, and
          // reload the transcript for whatever landed while we were away.
          if (msg.turn_running) s.resumeTurn();
          else if (s.turnRunning) s.endTurn();
          qc.invalidateQueries({ queryKey: qk.sessionMessages(msg.session_id) });
          break;
        }
        case "event": {
          routeEvent(msg.event, s);
          break;
        }
        case "approval_request": {
          s.addApproval({
            id: msg.id,
            command: msg.command,
            reason: msg.reason,
          });
          break;
        }
        case "turn_complete": {
          s.endTurn();
          const id = sessionIdRef.current;
          if (id) {
            qc.invalidateQueries({ queryKey: qk.sessionMessages(id) });
          }
          // agent may have touched git/files during the turn
          qc.invalidateQueries({ queryKey: qk.gitStatus });
          break;
        }
        case "turn_stopped": {
          s.stoppedTurn();
          const id = sessionIdRef.current;
          if (id) qc.invalidateQueries({ queryKey: qk.sessionMessages(id) });
          qc.invalidateQueries({ queryKey: qk.gitStatus });
          break;
        }
        case "error": {
          s.pushEvent({ kind: "error", message: msg.message });
          s.setActivity("failed", msg.message.slice(0, 60));
          s.endTurn();
          break;
        }
        case "pong":
          break;
        default:
          break;
      }
    };

    const routeEvent = (
      event: WsEvent,
      s: ReturnType<typeof useAgentStore.getState>,
    ) => {
      const kind = String(event.kind ?? "");

      /**
       * Move the runner, but only while a turn is actually live.
       *
       * Events reach the browser through a pump task while `turn_complete` is
       * sent directly on the socket, so a queued `TestsCompleted` can land
       * *after* the turn ended — which put the runner back into VERIFYING and
       * left it running forever with the answer already on screen. Query
       * invalidation below still runs for those late events.
       */
      if (useAgentStore.getState().turnRunning) applyActivity(kind, event, s);

      switch (kind) {
        case "ModelReasoningChunk":
          s.appendThinking(str(event.content));
          return;
        case "ModelStreamChunk":
          s.appendStreaming(str(event.content));
          return;
        case "TestsCompleted":
          s.setLatestTests({
            passed: Boolean(event.passed),
            passed_count: num(event.passed_count),
            failed_count: num(event.failed_count),
            at: Date.now(),
          });
          break;
        case "DesignUpdated":
        case "DesignCreated": {
          const designId = str(event.design_id);
          const store = useDesignStore.getState();
          // While the agent is drawing, follow the canvas it touches so the
          // user watches it build live instead of hunting for it in the list.
          // Off-turn (e.g. a background sync) we only refresh the open canvas.
          const follow = designId && useAgentStore.getState().turnRunning;
          if (follow && designId !== store.activeDesignId) {
            store.setActiveDesign(designId);
          }
          if (designId && (follow || designId === store.activeDesignId)) {
            qc.invalidateQueries({ queryKey: qk.design(designId) });
          }
          qc.invalidateQueries({ queryKey: qk.designs });
          break;
        }
        case "WorkspaceCreated":
        case "WorkspaceUpdated": {
          const workspaceId = str(event.workspace_id);
          // While the agent is writing, follow the workspace it touches so the
          // user watches the document appear rather than hunting for it.
          const open = useUIStore.getState().activeWorkspaceId;
          const follow = workspaceId && useAgentStore.getState().turnRunning;
          if (follow && !open) {
            useUIStore.getState().setActiveWorkspaceId(workspaceId);
          }
          if (workspaceId) {
            qc.invalidateQueries({ queryKey: qk.workspaceItem(workspaceId) });
            const path = str(event.path);
            if (path) {
              // The event carries a repository-relative path; the artifact
              // queries are keyed on the workspace-relative one, so refresh the
              // whole family rather than guessing the key.
              qc.invalidateQueries({ queryKey: ["workspaces", workspaceId, "artifact"] });
              qc.invalidateQueries({ queryKey: ["workspaces", workspaceId, "revisions"] });
            }
          }
          qc.invalidateQueries({ queryKey: qk.workspaces });
          break;
        }
        case "WorkspaceRunUpdated": {
          // The run row is the state; the event only says it moved. Refetch
          // rather than reconstructing a run from a stream of deltas.
          const workspaceId = str(event.workspace_id);
          if (workspaceId) {
            qc.invalidateQueries({ queryKey: qk.workspaceRun(workspaceId) });
            qc.invalidateQueries({ queryKey: qk.workspaceItem(workspaceId) });
            // A run works through the plan, so its progress is workspace
            // progress: the list's done-count has to follow it.
            qc.invalidateQueries({ queryKey: qk.workspaces });
          }
          break;
        }
        case "FileChanged":
          // Accumulate the live file list the panel shows while the turn runs.
          s.recordChange({
            path: str(event.path),
            action: str(event.action) || "changed",
            added: num(event.added),
            removed: num(event.removed),
          });
          qc.invalidateQueries({ queryKey: qk.gitStatus });
          break;
        case "GitChanged":
          qc.invalidateQueries({ queryKey: qk.gitStatus });
          break;
        case "TodoUpdated": {
          const todos = Array.isArray(event.todos)
            ? (event.todos as { content?: string; status?: string }[]).map((todo) => ({
                content: String(todo.content ?? ""),
                status: String(todo.status ?? "pending"),
              }))
            : [];
          // The checklist itself lives in the panel; the stream gets one line per
          // item that just finished, which is what the TUI shows too.
          const { completed, failed } = s.applyTodos(todos);
          for (const content of completed) s.pushEvent({ kind: "TodoCompleted", content });
          for (const content of failed) s.pushEvent({ kind: "TodoFailed", content });
          break;
        }
        case "PreviewStarted":
        case "PreviewStopped":
          qc.invalidateQueries({ queryKey: qk.previewStatus });
          break;
        default:
          break;
      }
      // record every event for the Output/timeline panels
      s.pushEvent(event);
    };

    connect();

    return () => {
      closedByUs = true;
      if (reconnectTimer) window.clearTimeout(reconnectTimer);
      if (pingRef.current) window.clearInterval(pingRef.current);
      wsRef.current?.close();
      wsRef.current = null;
    };
  }, [qc, target]);
}
