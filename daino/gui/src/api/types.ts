// Types mirroring the Daino REST + WebSocket API contract exactly.

export interface Health {
  status: string;
  version: string;
  project: string;
}

export interface ProjectInfo {
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
  /** Transcript entries. A long session carries more history into every prompt. */
  message_count: number;
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
  | "approval"
  | "diff"
  | "changeset"
  | "plan"
  | "test"
  | "checkpoint"
  | "deployment"
  | "mission_link";

/** One file in a turn's closing changeset, from the message's metadata. */
export interface ChangesetFile {
  path: string;
  change: "created" | "modified" | "deleted";
  added: number;
  removed: number;
}

export interface Changeset {
  files: ChangesetFile[];
  added: number;
  removed: number;
  verified: boolean | null;
}

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
  | "PreviewStopped"
  | "WorkspaceCreated"
  | "WorkspaceUpdated";

export interface WsEvent {
  kind: WsEventKind | string;
  [key: string]: unknown;
}

export type ServerSessionMessage =
  | { type: "session"; session_id: string; turn_running?: boolean }
  | { type: "event"; event: WsEvent }
  | { type: "approval_request"; id: string; command: string; reason: string }
  | { type: "turn_complete"; session_id: string }
  | { type: "turn_stopped"; session_id: string }
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

export type QASeverity = "critical" | "high" | "medium" | "low" | "info";
export type QAVerdict = "unknown" | "pass" | "warn" | "blocked";
export type QAScanProfile = "full" | "quality" | "security";

export type QAFindingCategory =
  | "secrets"
  | "vulnerability"
  | "dependencies"
  | "configuration"
  | "runtime"
  | "quality"
  | "tests"
  | "browser";

export interface QAFinding {
  id: string;
  title: string;
  severity: QASeverity;
  category: QAFindingCategory;
  source: string;
  location: string;
  line: number | null;
  detail: string;
  remediation: string;
  cwe: string;
  reference: string;
  confidence: "high" | "medium" | "low";
}

export interface QACheck {
  id: string;
  label: string;
  category:
    | "quality"
    | "tests"
    | "browser"
    | "dependencies"
    | "security"
    | "runtime";
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
  scan_profile: QAScanProfile;
  target_url: string;
  findings: QAFinding[];
  verdict: QAVerdict;
  gate_reasons: string[];
}

/** What the Inspector asks the backend to run. */
export interface RunInspectionRequest {
  profile: QAScanProfile;
  target_url: string;
  authorize_remote_target: boolean;
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

// ---- Settings (project/agent configuration; interface prefs stay local) ----

export interface ProviderInfo {
  name: string;
  type: string;
  base_url: string;
  model: string;
  /** Which configuration layer this provider is defined in. */
  scope: "project" | "global";
}

export interface ModelProfileInfo {
  name: string;
  provider: string;
  model: string;
  role: string;
  context_window: number;
  cost: string;
  latency: string;
  local: boolean;
}

export interface ProjectSettings {
  project: {
    name: string;
    default_mode: string;
    context_budget_tokens: number;
  };
  providers: ProviderInfo[];
  models: ModelProfileInfo[];
  /** Agent roles, in routing order: architect … debugger … deployer. */
  roles: string[];
  routing: Record<string, string>;
  runtime: {
    default: "local" | "docker" | "ssh";
    network_access: "restricted" | "allowed";
    docker_image: string;
    command_timeout_seconds: number;
  };
  security: {
    require_approval_for_install: boolean;
    require_approval_for_network: boolean;
    require_approval_for_production: boolean;
  };
  verification: {
    require_review: boolean;
    commands: string[];
  };
  observability: { log_level: string };
  memory: { enabled: boolean; auto_save: boolean };
  /** Hold an OS sleep inhibitor while the agent works. */
  keep_awake: boolean;
  notifications: {
    enabled: boolean;
    desktop: boolean;
    terminal_bell: boolean;
    on_completed: boolean;
    on_failed: boolean;
    on_approval: boolean;
  };
}

export interface SettingsPatch {
  keep_awake?: boolean;
  notifications_enabled?: boolean;
  notify_on_completed?: boolean;
  notify_on_failed?: boolean;
  notify_on_approval?: boolean;
  notify_desktop?: boolean;
  notify_terminal_bell?: boolean;
  routing?: Record<string, string>;
  default_provider?: string;
  runtime?: "local" | "docker" | "ssh";
  network_access?: "restricted" | "allowed";
  log_level?: "DEBUG" | "INFO" | "WARNING" | "ERROR" | "CRITICAL";
  require_approval_for_install?: boolean;
  require_approval_for_network?: boolean;
  require_approval_for_production?: boolean;
  require_review?: boolean;
}

export interface ProviderHealth extends ProviderInfo {
  connected: boolean;
  detail: string;
}

/** One provider as the agent panel's form describes it. */
export interface ProviderForm {
  name: string;
  type: "openrouter" | "ollama" | "vllm" | "openai-compatible";
  base_url: string;
  model: string;
  api_key: string;
  scope: "project" | "global";
  make_default: boolean;
}

export interface CatalogModel {
  id: string;
  name: string;
  detail: string;
}

/** One step of a provider test: endpoint, credentials, model, generation. */
export interface ProviderCheck {
  name: string;
  status: "pass" | "fail" | "skip";
  detail: string;
}

export interface ProviderTestResult {
  provider: ProviderHealth;
  checks: ProviderCheck[];
}

export interface ProviderSaveResult {
  provider: ProviderHealth;
  catalog: CatalogModel[];
  settings: ProjectSettings;
}

// ---- Agent customization (the browser's /mode, /effort, /verbose, /memory, /playbooks) ----

export interface AutonomyOption {
  id: "plan" | "ask" | "session" | "full";
  label: string;
  hint: string;
}

export interface InstructionFile {
  scope: "global" | "repository" | "scoped";
  label: string;
  path: string;
  /** Empty for the global file, which lives outside the repository. */
  relative_path: string;
  exists: boolean;
  bytes: number;
  editable_in_editor: boolean;
}

export interface PlaybookSummary {
  name: string;
  version: string;
  purpose: string;
  stages: string[];
  allowed_tools: string[];
  approval_points: string[];
  builtin: boolean;
  relative_path: string;
}

export interface AgentConfig {
  session_id: string;
  autonomy: { mode: AutonomyOption["id"]; options: AutonomyOption[] };
  effort: { value: string; options: string[] };
  verbose: boolean;
  roles: { role: string; profile: string }[];
  profiles: string[];
  instructions: { files: InstructionFile[]; max_bytes: number };
  playbooks: PlaybookSummary[];
  memory: { enabled: boolean; counts: Record<string, number>; total: number };
}

export interface MemoryItem {
  id: string;
  type: string;
  scope: string;
  status: string;
  content: string;
  summary: string;
  source: string;
  source_type: string;
  confidence: number;
  tags: string[];
  why: string[];
  created_at: string | null;
}

export interface EffectiveInstructions {
  target: string;
  text: string;
  sources: string[];
  scopes: Record<string, string[]>;
}

// ---- Workspaces (the WORKSPACE tab) ----
//
// A workspace is a named body of knowledge work: a goal, a real folder in the
// project, the documents in it, a plan, and the sources behind it.

export type WorkspaceTaskStatus =
  | "pending"
  | "in_progress"
  | "completed"
  | "failed";

export type ArtifactKind = "document" | "note" | "data" | "upload";

export interface WorkspaceTask {
  id: string;
  content: string;
  status: WorkspaceTaskStatus;
  position: number;
  notes: string;
  artifact_path: string;
  created_at: string;
  updated_at: string;
}

export interface Artifact {
  /** Relative to the workspace folder, e.g. "findings.md". */
  path: string;
  /** Relative to the repository root — what the file APIs accept. */
  repo_path: string;
  title: string;
  kind: ArtifactKind;
  suffix: string;
  bytes: number;
  updated_at: string;
  preview: string;
  revisions: number;
  /** Set on an upload that needed a parser: where its markdown ended up. */
  extracted_path: string;
  /** Why an upload is unreadable, when it is. */
  warning: string;
}

export interface ArtifactContent {
  artifact: Artifact;
  content: string;
  readable: boolean;
}

export interface ArtifactRevision {
  version: number;
  path: string;
  author: "user" | "agent" | "unknown";
  bytes: number;
  saved_at: string;
}

export interface ResearchSource {
  id: string;
  url: string;
  title: string;
  snippet: string;
  cache_path: string;
  retrieved_at: string;
}

export interface Workspace {
  id: string;
  name: string;
  slug: string;
  goal: string;
  kind: string;
  folder: string;
  status: "active" | "archived";
  tasks: WorkspaceTask[];
  artifacts: Artifact[];
  uploads: Artifact[];
  sources: ResearchSource[];
  /** The conversation attached, so the agent panel can follow this workspace. */
  session_id: string;
  metadata: Record<string, unknown>;
  created_at: string;
  updated_at: string;
}

export interface WorkspaceSummary {
  id: string;
  name: string;
  slug: string;
  goal: string;
  kind: string;
  folder: string;
  status: "active" | "archived";
  artifact_count: number;
  upload_count: number;
  task_count: number;
  done_count: number;
  updated_at: string;
}

export interface StarterArtifact {
  title: string;
  filename: string;
  outline: string[];
}

export interface WorkspaceTemplate {
  name: string;
  title: string;
  purpose: string;
  starter_tasks: string[];
  starter_artifacts: StarterArtifact[];
  preamble: string;
}

export interface CreateWorkspaceRequest {
  name: string;
  goal: string;
  kind: string;
  folder: string;
}

