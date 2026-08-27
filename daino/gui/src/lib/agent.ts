// Send a chat message to the agent over the shared session websocket,
// prepending the context block (from the ContextBar chips) unless disabled.
import { useAgentStore } from "../store/agentStore";
import { useSettingsStore } from "../store/settingsStore";
import { useUIStore } from "../store/uiStore";
import { composeMessage } from "./context";

export function sendChatMessage(
  text: string,
  opts?: { withContext?: boolean },
): boolean {
  const state = useAgentStore.getState();
  if (!state.send || !text.trim() || state.turnRunning) return false;

  const workspace = useUIStore.getState().activeWorkspaceTab;
  // An explicit argument wins; otherwise Settings ▸ Agent decides whether the
  // open file, selection, and diff ride along with the message.
  const withContext =
    opts?.withContext ?? useSettingsStore.getState().sendWithContext;
  const finalText = withContext
    ? composeMessage(state.chips, workspace, text)
    : text;

  state.send({
    type: "user_message",
    text: finalText,
    profile: state.selectedModel ?? undefined,
  });
  state.beginTurn(text);
  if (withContext) state.clearChips();
  return true;
}
