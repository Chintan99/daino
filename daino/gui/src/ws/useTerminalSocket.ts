// Per-terminal WebSocket wiring xterm <-> backend PTY.
import { useEffect, useRef } from "react";
import type { Terminal } from "@xterm/xterm";
import type { ServerTerminalMessage } from "../api/types";
import { wsUrl } from "./url";

export interface TerminalSocket {
  sendInput: (data: string) => void;
  sendResize: (rows: number, cols: number) => void;
}

export function useTerminalSocket(
  id: string,
  term: Terminal | null,
  onExit?: () => void,
) {
  const wsRef = useRef<WebSocket | null>(null);

  useEffect(() => {
    if (!term) return;
    const ws = new WebSocket(wsUrl(`/ws/terminal/${id}`));
    wsRef.current = ws;

    ws.onmessage = (ev) => {
      let msg: ServerTerminalMessage;
      try {
        msg = JSON.parse(ev.data as string);
      } catch {
        return;
      }
      if (msg.type === "output") term.write(msg.data);
      else if (msg.type === "exit") {
        term.write("\r\n\x1b[90m[process exited]\x1b[0m\r\n");
        onExit?.();
      } else if (msg.type === "error") {
        term.write(`\r\n\x1b[31m${msg.message}\x1b[0m\r\n`);
      }
    };

    const dataSub = term.onData((data) => {
      if (ws.readyState === WebSocket.OPEN)
        ws.send(JSON.stringify({ type: "input", data }));
    });

    const resizeSub = term.onResize(({ rows, cols }) => {
      if (ws.readyState === WebSocket.OPEN)
        ws.send(JSON.stringify({ type: "resize", rows, cols }));
    });

    return () => {
      dataSub.dispose();
      resizeSub.dispose();
      ws.close();
      wsRef.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id, term]);
}
