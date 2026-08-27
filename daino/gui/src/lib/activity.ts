// What D[Ai]NO is doing, in the terminal client's own vocabulary.
//
// The TUI's task checklist (daino/tui/widgets/checklist.py) drives a dinosaur
// runner from these states, and the mappings below are the same ones its
// `_role_activity` / `_tool_activity` use. Keeping them identical means the two
// clients never disagree about whether the agent is thinking, building, or has
// failed — and the browser's runner animates on exactly the same cues.

export type ActivityState =
  | "idle"
  | "working"
  | "thinking"
  | "planning"
  | "inspecting"
  | "building"
  | "verifying"
  | "completed"
  | "failed";

export const ACTIVITY_LABELS: Record<ActivityState, string> = {
  idle: "READY",
  working: "WORKING",
  thinking: "THINKING",
  planning: "PLANNING",
  inspecting: "INSPECTING",
  building: "BUILDING",
  verifying: "VERIFYING",
  completed: "TASK COMPLETED",
  failed: "ERROR",
};

/** The states during which the runner actually runs. */
export const RUNNING_STATES: ReadonlySet<ActivityState> = new Set<ActivityState>([
  "working",
  "thinking",
  "planning",
  "inspecting",
  "building",
  "verifying",
]);

export const isRunning = (state: ActivityState) => RUNNING_STATES.has(state);

/** Which token colours each state, matching the TUI's palette roles. */
export const ACTIVITY_COLOR: Record<ActivityState, string> = {
  idle: "var(--text-3)",
  working: "var(--accent)",
  thinking: "var(--purple)",
  planning: "var(--purple)",
  inspecting: "var(--blue)",
  building: "var(--accent)",
  verifying: "var(--yellow)",
  completed: "var(--green)",
  failed: "var(--red)",
};

const ROLE_ACTIVITY: Record<string, ActivityState> = {
  architect: "thinking",
  planner: "planning",
  builder: "building",
  debugger: "inspecting",
  reviewer: "inspecting",
  tester: "verifying",
  summarizer: "thinking",
  deployer: "building",
};

export function roleActivity(role: string): ActivityState {
  return ROLE_ACTIVITY[role.toLowerCase()] ?? "thinking";
}

export function toolActivity(tool: string): ActivityState {
  const lowered = tool.toLowerCase();
  const has = (...needles: string[]) => needles.some((item) => lowered.includes(item));
  if (has("test", "verify", "lint", "typecheck", "build")) return "verifying";
  if (has("write", "replace", "edit", "delete", "patch")) return "building";
  if (has("read", "search", "grep", "glob", "list", "memory")) return "inspecting";
  return "thinking";
}
