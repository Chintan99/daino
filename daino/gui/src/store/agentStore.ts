// Agent session state: websocket status, live events, approvals, context chips.
import { create } from "zustand";
import type { ClientSessionMessage, WsEvent } from "../api/types";
import { RUNNING_STATES, type ActivityState } from "../lib/activity";

export type WsStatus = "connecting" | "open" | "closed" | "error";

export interface ApprovalRequest {
  id: string;
  command: string;
  reason: string;
}

// A friendly, rendered live event (never raw JSON shown to the user).
export interface LiveEvent {
  id: number;
  kind: string;
  event: WsEvent;
  at: number;
}

/** What the agent is doing right now, shown by the runner in the agent panel. */
export interface Activity {
  state: ActivityState;
  detail: string;
}

/** One file the running turn has edited so far. */
export interface LiveChange {
  path: string;
  action: string;
  added: number;
  removed: number;
  at: number;
}

/** One checklist item, as the agent maintains it. */
export interface Todo {
  content: string;
  status: string;
}

export interface TestsResult {
  passed: boolean;
  passed_count: number;
  failed_count: number;
  at: number;
}

export type ChipKind =
  | "active_file"
  | "selection"
  | "design_node"
  | "terminal"
  | "git_diff"
  | "attachment";

export interface ContextChip {
  id: string;
  kind: ChipKind;
  label: string;
  payload: Record<string, unknown>;
}

const EVENT_CAP = 400;
let eventSeq = 0;

interface AgentState {
  sessionId: string | null;
  wsStatus: WsStatus;
  turnRunning: boolean;

  events: LiveEvent[];
  liveStart: number; // index into events where the current turn began
  pendingUser: string | null; // optimistic user message for the running turn
  thinking: string; // ephemeral reasoning
  streaming: string; // ephemeral answer stream (ask turns)
  approvals: ApprovalRequest[];
  latestTests: TestsResult | null;
  activity: Activity;
  /** Files edited by the turn in flight, newest last. Cleared when one starts. */
  liveChanges: LiveChange[];
  /** The agent's current checklist for this session. */
  todos: Todo[];

  chips: ContextChip[];
  selectedModel: string | null;

  send: ((msg: ClientSessionMessage) => void) | null;

  setSession: (id: string) => void;
  setWsStatus: (s: WsStatus) => void;
  setSend: (fn: ((msg: ClientSessionMessage) => void) | null) => void;

  pushEvent: (event: WsEvent) => void;
  appendThinking: (text: string) => void;
  appendStreaming: (text: string) => void;
  resetEphemeral: () => void;
  setTurnRunning: (v: boolean) => void;
  beginTurn: (userText: string) => void;
  endTurn: () => void;
  /** Re-enter the running state for a turn this client did not start. */
  resumeTurn: () => void;
  /** Drop everything that belonged to the conversation being left. */
  resetForSession: () => void;
  setLatestTests: (t: TestsResult) => void;
  setActivity: (state: ActivityState, detail?: string) => void;
  recordChange: (change: Omit<LiveChange, "at">) => void;
  /** Replace the checklist, returning the items that just changed state. */
  applyTodos: (todos: Todo[]) => { completed: string[]; failed: string[] };

  addApproval: (a: ApprovalRequest) => void;
  removeApproval: (id: string) => void;

  addChip: (chip: ContextChip) => void;
  removeChip: (id: string) => void;
  clearChips: () => void;

  setModel: (m: string | null) => void;
}

export const useAgentStore = create<AgentState>((set, get) => ({
  sessionId: null,
  wsStatus: "connecting",
  turnRunning: false,

  events: [],
  liveStart: 0,
  pendingUser: null,
  thinking: "",
  streaming: "",
  approvals: [],
  latestTests: null,
  activity: { state: "idle", detail: "" },
  liveChanges: [],
  todos: [],

  chips: [],
  selectedModel: null,

  send: null,

  setSession: (id) => set({ sessionId: id }),
  setWsStatus: (wsStatus) => set({ wsStatus }),
  setSend: (fn) => set({ send: fn }),

  pushEvent: (event) =>
    set((s) => {
      const next: LiveEvent = {
        id: eventSeq++,
        kind: String(event.kind ?? "unknown"),
        event,
        at: Date.now(),
      };
      const events = [...s.events, next];
      if (events.length > EVENT_CAP) events.splice(0, events.length - EVENT_CAP);
      return { events };
    }),

  appendThinking: (text) => set((s) => ({ thinking: s.thinking + text })),
  appendStreaming: (text) => set((s) => ({ streaming: s.streaming + text })),
  resetEphemeral: () => set({ thinking: "", streaming: "" }),
  setTurnRunning: (turnRunning) => set({ turnRunning }),
  beginTurn: (userText) =>
    set((s) => ({
      turnRunning: true,
      pendingUser: userText,
      liveStart: s.events.length,
      thinking: "",
      streaming: "",
      // The same opening state the TUI shows when a request arrives.
      activity: { state: "thinking", detail: "understanding request" },
      // Last turn's file list and plan are not this turn's.
      liveChanges: [],
      todos: [],
    })),

  /**
   * Adopt a turn that is already running on the server.
   *
   * A page refresh loses the client's state but not the work: the server keeps
   * the turn going and reports it on connect. Without this the browser showed an
   * idle agent while files were being written.
   */
  resumeTurn: () =>
    set((s) =>
      s.turnRunning
        ? {}
        : {
            turnRunning: true,
            liveStart: s.events.length,
            activity: { state: "working", detail: "resumed after reload" },
          },
    ),
  endTurn: () =>
    set((s) => ({
      turnRunning: false,
      pendingUser: null,
      thinking: "",
      streaming: "",
      // A turn that ended after a failure keeps the failure visible; anything
      // else reads as complete until the next turn starts.
      activity:
        s.activity.state === "failed"
          ? s.activity
          : { state: "completed", detail: s.activity.detail },
      // The checklist belongs to the turn that made it. Keeping it afterwards
      // left a stale, permanently half-finished plan on screen.
      todos: [],
    })),
  setLatestTests: (latestTests) => set({ latestTests }),
  /**
   * The invariant the runner depends on: a running state requires a live turn.
   *
   * Enforced here as well as at the socket, because "the dinosaur is running
   * but nothing is happening" is the one failure of this widget a user cannot
   * dismiss, and it only takes one late event to cause it.
   */
  /**
   * Accumulate an edit into the live file list.
   *
   * Keyed by path with the counts summed, because a turn commonly edits the same
   * file twice and two rows for one file reads as two files. The row moves to
   * the end so "what is it working on now" is the last line.
   */
  recordChange: ({ path, action, added, removed }) =>
    set((s) => {
      const existing = s.liveChanges.find((change) => change.path === path);
      const others = s.liveChanges.filter((change) => change.path !== path);
      return {
        liveChanges: [
          ...others,
          {
            path,
            action,
            added: (existing?.added ?? 0) + added,
            removed: (existing?.removed ?? 0) + removed,
            at: Date.now(),
          },
        ],
      };
    }),

  applyTodos: (todos) => {
    const previous = new Map(get().todos.map((todo) => [todo.content, todo.status]));
    const completed: string[] = [];
    const failed: string[] = [];
    for (const todo of todos) {
      const was = previous.get(todo.content);
      if (todo.status === "completed" && was !== "completed") completed.push(todo.content);
      if (todo.status === "failed" && was !== "failed") failed.push(todo.content);
    }
    set({ todos });
    return { completed, failed };
  },

  /**
   * Clear per-conversation state when the attached session changes.
   *
   * The transcript is refetched by id, but the live view is not: leaving the
   * previous conversation's events, plan, file list and context chips on screen
   * would attribute them to the new one.
   */
  resetForSession: () =>
    set({
      events: [],
      liveStart: 0,
      pendingUser: null,
      thinking: "",
      streaming: "",
      approvals: [],
      latestTests: null,
      activity: { state: "idle", detail: "" },
      liveChanges: [],
      todos: [],
      chips: [],
      turnRunning: false,
      // A fresh conversation starts on Auto. Carrying the previous session's
      // pinned profile over is how a brand-new session ended up pinned — and a
      // pinned session is excluded from escalation, so a stalled turn there has
      // no way to recover.
      selectedModel: null,
    }),

  setActivity: (state, detail = "") =>
    set((s) =>
      !s.turnRunning && RUNNING_STATES.has(state) ? {} : { activity: { state, detail } },
    ),

  addApproval: (a) =>
    set((s) =>
      s.approvals.some((x) => x.id === a.id)
        ? {}
        : { approvals: [...s.approvals, a] },
    ),
  removeApproval: (id) =>
    set((s) => ({ approvals: s.approvals.filter((a) => a.id !== id) })),

  addChip: (chip) =>
    set((s) => {
      const chips = s.chips.filter((c) => c.id !== chip.id);
      return { chips: [...chips, chip] };
    }),
  removeChip: (id) =>
    set((s) => ({ chips: s.chips.filter((c) => c.id !== id) })),
  clearChips: () => set({ chips: [] }),

  setModel: (selectedModel) => set({ selectedModel }),
}));
