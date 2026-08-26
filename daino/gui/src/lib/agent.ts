// Send a chat message to the agent over the shared session websocket,
// prepending the context block (from the ContextBar chips) unless disabled.
import { useAgentStore } from "../store/agentStore";
import { useUIStore } from "../store/uiStore";
import { composeMessage } from "./context";

export function sendChatMessage(
  text: string,
  opts?: { withContext?: boolean },
): boolean {
  const state = useAgentStore.getState();
  if (!state.send || !text.trim() || state.turnRunning) return false;

  const workspace = useUIStore.getState().activeWorkspaceTab;
  const withContext = opts?.withContext !== false;
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
