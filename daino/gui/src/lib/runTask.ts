// Running one of the project's declared commands.
//
// A task becomes a process in a terminal, not in a hidden subprocess. That is
// deliberate: these command strings come out of files in the repository, and the
// moment one starts running should be somewhere the user can see it, interrupt
// it, and read its output — which is what a terminal already is.
import { api } from "../api/client";
import { useUIStore } from "../store/uiStore";
import { useTerminalStore } from "../store/terminalStore";
import type { ProjectTask } from "../api/types";

/** Open a terminal, make it visible, and send the task's command to it. */
export async function openTerminalWith(task: ProjectTask): Promise<void> {
  const ui = useUIStore.getState();
  ui.setActiveWorkspaceTab("code");
  ui.setBottomTab("terminal");
  try {
    const created = await api.createTerminal();
    useTerminalStore.getState().addTerminal(created.id);
    // The newline is what runs it; without it the command just sits there
    // looking like the click did nothing.
    useTerminalStore.getState().queueInput(created.id, `${task.command}\n`);
  } catch (err) {
    window.alert(
      `Could not start "${task.label}": ${
        err instanceof Error ? err.message : String(err)
      }`,
    );
  }
}
