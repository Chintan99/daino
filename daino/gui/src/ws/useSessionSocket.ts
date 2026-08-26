// Single shared session WebSocket. Mounted once in AppShell.
// Feeds live events into the agent store and invalidates queries on turn_complete
// and DesignUpdated so React Query re-fetches persisted state.
import { useEffect, useRef } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { qk } from "../api/hooks";
import { useAgentStore } from "../store/agentStore";
import { useDesignStore } from "../store/designStore";
import type {
  ClientSessionMessage,
  ServerSessionMessage,
  WsEvent,
} from "../api/types";
import { wsUrl } from "./url";

function num(v: unknown): number {
  return typeof v === "number" ? v : 0;
}
function str(v: unknown): string {
  return typeof v === "string" ? v : "";
}

export function useSessionSocket(target: string = "latest") {
  const qc = useQueryClient();
  const wsRef = useRef<WebSocket | null>(null);
  const sessionIdRef = useRef<string | null>(null);
  const pingRef = useRef<number | null>(null);

  useEffect(() => {
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
        case "error": {
          s.pushEvent({ kind: "error", message: msg.message });
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
          const active = useDesignStore.getState().activeDesignId;
          if (designId && designId === active) {
            qc.invalidateQueries({ queryKey: qk.design(designId) });
          }
          qc.invalidateQueries({ queryKey: qk.designs });
          break;
        }
        case "FileChanged":
        case "GitChanged":
          qc.invalidateQueries({ queryKey: qk.gitStatus });
          break;
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
