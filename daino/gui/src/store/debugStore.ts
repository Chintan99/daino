// The debug session as the panel renders it.
//
// A thin mirror of the server's state rather than a second source of truth: the
// session lives on the server so it survives a page reload, and this holds the
// last thing it said. The one piece of genuinely local state is which stack
// frame the user has selected, since that is a view concern.
import { create } from "zustand";
import type {
  DebugBreakpoint,
  DebugSessionInfo,
  DebugStatus,
} from "../api/types";

interface DebugState {
  session: DebugSessionInfo | null;
  breakpoints: DebugBreakpoint[];
  running: boolean;
  /** Which frame's variables are shown. Local: it is a view choice. */
  selectedFrameId: number | null;
  apply: (status: DebugStatus) => void;
  selectFrame: (id: number | null) => void;
}

export const useDebugStore = create<DebugState>((set) => ({
  session: null,
  breakpoints: [],
  running: false,
  selectedFrameId: null,
  apply: (status) =>
    set((s) => ({
      session: status.session,
      breakpoints: status.breakpoints,
      running: status.running,
      // Default to the innermost frame — where execution actually is, and what
      // someone stopping at a breakpoint wants to look at first.
      selectedFrameId:
        status.session?.frames.some((f) => f.id === s.selectedFrameId)
          ? s.selectedFrameId
          : (status.session?.frames[0]?.id ?? null),
    })),
  selectFrame: (selectedFrameId) => set({ selectedFrameId }),
}));

/** Breakpoints in one file, keyed for quick gutter lookup. */
export function breakpointsFor(
  breakpoints: DebugBreakpoint[],
  path: string,
): DebugBreakpoint[] {
  return breakpoints.filter((item) => item.path === path);
}
