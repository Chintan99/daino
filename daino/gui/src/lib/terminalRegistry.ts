// Live xterm instances by terminal id, so the Terminal menu can clear or
// re-fit the shell the user is looking at without prop-drilling through the
// bottom panel.
import type { Terminal } from "@xterm/xterm";

const terminals = new Map<string, Terminal>();

export function registerTerminal(id: string, term: Terminal): () => void {
  terminals.set(id, term);
  return () => {
    if (terminals.get(id) === term) terminals.delete(id);
  };
}

export function getTerminal(id: string | null): Terminal | null {
  return id ? terminals.get(id) ?? null : null;
}
