// Types mirroring the Daino REST + WebSocket API contract exactly.

export interface Health {
  status: string;
  version: string;
  project: string;
}

export interface Workspace {
  name: string;
  root: string;
  runtime: string;
  models: string[];
  routing: Record<string, unknown>;
}

export interface SessionSummary {
  id: string;
  title: string;
  active_model: string;
  updated_at: string;
  context_files: string[];
}

export interface SessionList {
  sessions: SessionSummary[];
}

export type MessageKind =
  | "user"
  | "agent"
  | "tool"
  | "error"
  | "status"
  | "summary"
  | "approval";

export interface SessionMessage {
  id: string;
  kind: MessageKind;
  role: string;
  content: string;
  created_at: string;
  metadata: Record<string, unknown>;
}

export interface SessionMessages {
  session_id: string;
  messages: SessionMessage[];
}

// ---- Files ----

export type EntryType = "file" | "directory";

export interface TreeEntry {
  name: string;
  path: string;
  type: EntryType;
}

export interface FileTree {
  path: string;
  entries: TreeEntry[];
}

export interface FileRead {
  path: string;
  content: string;
  hash: string;
  mtime: number;
  language: string;
  size: number;
}

export interface FileWriteResult {
  path: string;
  hash: string;
}

export interface SearchMatch {
  path: string;
  line: number;
  text: string;
}

export interface SearchResult {
  query: string;
  matches: SearchMatch[];
  success: boolean;
}

// ---- Git ----

export interface GitEntry {
  path: string;
  status?: string;
}

export interface GitStatus {
  repository: boolean;
  branch: string;
  staged: GitEntry[];
  modified: GitEntry[];
  untracked: GitEntry[];
}

export interface GitDiff {
  repository: boolean;
  path: string;
  staged: boolean;
  diff: string;
}

// ---- Designs ----

export type DesignType =
  | "architecture"
  | "flowchart"
  | "database"
  | "api_flow"
  | "ui"
  | "prototype";

export interface DesignNode {
  id: string;
  label: string;
  type: string;
  position: { x: number; y: number };
  data: Record<string, unknown>;
}

export interface DesignEdge {
  id: string;
  source: string;
  target: string;
  label?: string;
  data?: Record<string, unknown>;
}

export interface Design {
  id: string;
  name: string;
  type: DesignType | string;
  version: number;
  nodes: DesignNode[];
  edges: DesignEdge[];
  frames: unknown[];
  metadata: Record<string, unknown>;
  created_at: string;
  updated_at: string;
}

export interface DesignSummary {
  id: string;
  name: string;
  type: string;
  version: number;
  node_count: number;
  edge_count: number;
  frame_count: number;
  updated_at: string;
}

export interface DesignList {
  designs: DesignSummary[];
}

// ---- Preview ----

export interface PreviewCommand {
  label: string;
  command: string;
  source: string;
  default_url: string;
}

export interface PreviewDetect {
  commands: PreviewCommand[];
}

export interface PreviewStatus {
  running: boolean;
  command: string;
  url: string;
  logs: string[];
}

// ---- Terminals ----

export interface TerminalCreated {
  id: string;
  cwd: string;
}

export interface TerminalList {
  terminals: string[];
}

// ---- WebSocket messages ----

export type WsEventKind =
  | "MissionCreated"
  | "MissionStarted"
  | "MissionCompleted"
  | "MissionFailed"
  | "TaskStarted"
  | "TaskCompleted"
  | "TodoUpdated"
  | "ContextCompacted"
  | "AgentRoleChanged"
  | "ModelSelected"
  | "ModelStreamChunk"
  | "ModelReasoningChunk"
  | "ToolStarted"
  | "ToolProgress"
  | "ToolCompleted"
  | "ToolFailed"
  | "FileChanged"
  | "TestsStarted"
  | "TestsCompleted"
  | "ApprovalRequested"
  | "ApprovalResolved"
  | "DesignCreated"
  | "DesignUpdated"
  | "GitChanged"
  | "PreviewStarted"
  | "PreviewStopped";

export interface WsEvent {
  kind: WsEventKind | string;
  [key: string]: unknown;
}

export type ServerSessionMessage =
  | { type: "session"; session_id: string }
  | { type: "event"; event: WsEvent }
  | { type: "approval_request"; id: string; command: string; reason: string }
  | { type: "turn_complete"; session_id: string }
  | { type: "error"; message: string }
  | { type: "pong" };

export type ClientSessionMessage =
  | { type: "user_message"; text: string; profile?: string }
  | { type: "approval_resolve"; id: string; approved: boolean; remember: boolean }
  | { type: "cancel" }
  | { type: "ping" };

export type ServerTerminalMessage =
  | { type: "output"; data: string }
  | { type: "exit" }
  | { type: "error"; message: string };

export type ClientTerminalMessage =
  | { type: "input"; data: string }
  | { type: "resize"; rows: number; cols: number };
