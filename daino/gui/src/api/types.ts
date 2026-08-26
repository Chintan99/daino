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

// ---- Git (VSCode-style whole-file diff) ----

export interface GitFileDiff {
  repository: boolean;
  path: string;
  staged: boolean;
  original: string;
  modified: string;
  language: string;
  binary: boolean;
}

// ---- Execution map ----

export interface ModelUsage {
  provider: string;
  model: string;
  role: string;
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  estimated_cost: number;
  latency_ms: number;
  success: boolean;
}

export interface TraceStep {
  id: string;
  kind: string;
  title: string;
  detail: string;
  status: string;
  timestamp: string;
  target: string;
  duration_seconds: number;
  model_usage: ModelUsage | null;
}

export interface ExecutionPrompt {
  mission_id: string;
  request: string;
  title: string;
  status: string;
  created_at: string;
  total_tokens: number;
  estimated_cost: number;
  step_count: number;
  tool_count: number;
  model_call_count: number;
}

export interface ExecutionTrace {
  mission_id: string;
  request: string;
  status: string;
  created_at: string;
  total_input_tokens: number;
  total_output_tokens: number;
  total_tokens: number;
  estimated_cost: number;
  total_model_latency_ms: number;
  total_tool_duration_seconds: number;
  model_call_count: number;
  tool_count: number;
  steps: TraceStep[];
}

// ---- Audit log ----

export interface AuditEvent {
  timestamp?: string;
  event?: string;
  mission_id?: string;
  [key: string]: unknown;
}

export interface AuditLogPage {
  total: number;
  matched: number;
  events: AuditEvent[];
}

// ---- QA ----

export type QAStatus =
  | "pending"
  | "running"
  | "passed"
  | "failed"
  | "skipped"
  | "completed"
  | "cancelled";

export interface QACheck {
  id: string;
  label: string;
  category: "quality" | "tests" | "browser" | "dependencies";
  command: string;
  status: QAStatus;
  summary: string;
  output: string;
  duration_seconds: number;
  network_required: boolean;
}

export interface QASpecialist {
  id: string;
  label: string;
  role: string;
  objective: string;
  status: QAStatus;
  summary: string;
  steps: number;
  error: string;
}

export interface QAReport {
  id: string;
  status: QAStatus;
  started_at: string;
  finished_at: string | null;
  project_root: string;
  project_profile: string[];
  checks: QACheck[];
  specialists: QASpecialist[];
  summary: string;
  mission_id: string;
}

export interface QALatest {
  running: boolean;
  report: QAReport | null;
}

export interface QAHistory {
  running: boolean;
  reports: QAReport[];
}

// ---- Missions, checkpoints, approvals, repository ----

export interface MissionSummary {
  id: string;
  title: string;
  status: string;
  mode: string;
  updated_at: string;
  branch: string;
  workspace: string;
  task_counts: Record<string, number>;
}

export interface MissionDetails {
  mission: Record<string, unknown>;
  requirements: Record<string, unknown>;
  tasks: { id: string; title: string; status: string; risk_level: string }[];
  tools: { tool: string; summary: string; success: boolean; duration: number }[];
  tests: unknown[];
  reviews: unknown[];
  approvals: { category: string; subject: string; approved: boolean }[];
  checkpoints: { id: string; description: string; revision: string }[];
}

export interface CheckpointEntry {
  id: string;
  mission_id: string;
  revision: string;
  description: string;
  created_at: string | null;
}

export interface ApprovalEntry {
  id: string;
  mission_id: string;
  category: string;
  subject: string;
  approved: boolean;
  approver: string;
  created_at: string | null;
}

export interface RepositoryInfo {
  summary: string;
  file_count: number;
  languages: Record<string, number>;
  frameworks: string[];
  entrypoints: string[];
  routes: unknown[];
  database_models: unknown[];
  tests: string[];
  dependencies: Record<string, unknown>;
  generated_at: string;
}

// ---- Documentation ----

export interface DocsPageSummary {
  slug: string;
  title: string;
  section: string;
}

export interface DocsIndex {
  available: boolean;
  project: string;
  pages: DocsPageSummary[];
}

export interface DocsPage {
  slug: string;
  title: string;
  markdown: string;
}
