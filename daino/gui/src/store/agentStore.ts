// Agent session state: websocket status, live events, approvals, context chips.
import { create } from "zustand";
import type { ClientSessionMessage, WsEvent } from "../api/types";

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
  | "git_diff";

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
  setLatestTests: (t: TestsResult) => void;

  addApproval: (a: ApprovalRequest) => void;
  removeApproval: (id: string) => void;

  addChip: (chip: ContextChip) => void;
  removeChip: (id: string) => void;
  clearChips: () => void;

  setModel: (m: string | null) => void;
}

export const useAgentStore = create<AgentState>((set) => ({
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
    })),
  endTurn: () =>
    set({ turnRunning: false, pendingUser: null, thinking: "", streaming: "" }),
  setLatestTests: (latestTests) => set({ latestTests }),

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
