// Terminal lifecycle, shared by the bottom panel and the Terminal menu.
//
// Creation is guarded because two callers (the panel's auto-start and a menu
// command) can race, and each unguarded call leaks a real PTY on the backend.
import { api, ApiError } from "../api/client";
import { useTerminalStore } from "../store/terminalStore";
import { useUIStore } from "../store/uiStore";

let creating = false;

export async function createTerminal(options?: { reveal?: boolean }): Promise<string | null> {
  if (creating) return null;
  creating = true;
  try {
    const res = await api.createTerminal();
    useTerminalStore.getState().addTerminal(res.id);
    if (options?.reveal !== false) {
      useUIStore.getState().setActiveWorkspaceTab("code");
      useUIStore.getState().setBottomTab("terminal");
    }
    return res.id;
  } catch (err) {
    // The backend caps concurrent shells per project; say so instead of
    // leaving the panel on "Starting a terminal…" forever.
    useTerminalStore
      .getState()
      .setError(err instanceof ApiError ? err.message : String(err));
    return null;
  } finally {
    creating = false;
  }
}

export async function closeTerminal(id: string): Promise<void> {
  useTerminalStore.getState().removeTerminal(id);
  try {
    await api.deleteTerminal(id);
  } catch {
    /* the session is gone from the UI either way */
  }
}

/**
 * Show the shells this project already has, or open the first one.
 *
 * Adopting them matters on a reload: the PTYs outlive the page, so re-attaching
 * returns the user to their shell with its scrollback instead of leaving the old
 * one orphaned and starting yet another.
 */
export async function restoreTerminals(): Promise<void> {
  const store = useTerminalStore.getState();
  if (store.ids.length) return;
  try {
    const existing = await api.listTerminals();
    if (existing.terminals.length) {
      for (const id of existing.terminals) useTerminalStore.getState().addTerminal(id);
      return;
    }
  } catch {
    /* fall through to creating one */
  }
  await createTerminal({ reveal: false });
}
