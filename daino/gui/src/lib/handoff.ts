// A chat message addressed to a conversation the shared socket is not on yet.
//
// The Workspace tab borrows the one agent socket while it is mounted, so a
// handoff that sends first and switches tabs second lands its brief in the
// workspace's conversation and then drops the user into CODE's. Queueing puts
// the two halves in the right order: point the socket at the target, leave the
// tab, and let the socket send once it is actually connected there.
import { useAgentStore } from "../store/agentStore";
import { useUIStore } from "../store/uiStore";

/** Keyed by socket target ("latest" or a session id), never by a resolved id. */
let pending: { target: string; text: string } | null = null;

function deliverNow(text: string): boolean {
  const state = useAgentStore.getState();
  if (!state.send || state.turnRunning) return false;
  state.send({
    type: "user_message",
    text,
    profile: state.selectedModel ?? undefined,
  });
  state.beginTurn(text);
  return true;
}

/**
 * Hand this brief to the conversation CODE will show, and go there.
 *
 * The shelved target is the conversation the Workspace tab displaced when it
 * took the socket — which is exactly the one CODE returns to. Unshelving points
 * the socket back at it; the queued message rides the reconnection, so the work
 * starts in the conversation the user is about to be looking at.
 */
export function handOffToCode(text: string): void {
  const ui = useUIStore.getState();
  const target = ui.sessionShelved ? ui.shelvedSessionTarget : ui.sessionTarget;
  const key = target ?? "latest";
  const current = ui.sessionTarget ?? "latest";
  if (key === current && deliverNow(text)) return;
  pending = { target: key, text };
  ui.unshelveSessionTarget();
  // A workspace that never shelved anything still has to end up on `target`.
  if (useUIStore.getState().sessionTarget !== target) {
    useUIStore.getState().setSessionTarget(target);
  }
}

/**
 * Send the message queued for this socket target, if it can be sent right now.
 *
 * Called by the session socket once the server has confirmed the session, and
 * again every time a turn ends. The second call is the one that matters: the
 * destination conversation is frequently mid-turn when the handoff arrives, and
 * this used to claim the message anyway and then discard it because it could
 * not be delivered — the brief simply vanished, with nothing shown to the user.
 * A message that cannot be sent yet stays queued and goes out at the next turn
 * boundary instead.
 */
export function flushQueuedMessage(target: string): void {
  if (pending === null || pending.target !== target) return;
  const { text } = pending;
  if (!deliverNow(text)) return;
  pending = null;
}

/**
 * Whether a handoff has pointed the socket somewhere and is waiting on it.
 *
 * The Workspace tab checks this on unmount: a handoff has already decided which
 * conversation the user should land in, so restoring the shelved one on the way
 * out would undo exactly what the handoff set up.
 */
export function handoffInFlight(): boolean {
  return pending !== null;
}

/** Drop anything queued — used when a handoff is superseded or cancelled. */
export function clearQueuedMessage(): void {
  pending = null;
}
