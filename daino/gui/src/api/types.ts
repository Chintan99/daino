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

// ---- Debugging (CODE ▸ Debug) ----

export type DebugState =
  | "starting"
  | "running"
  | "stopped"
  | "terminated"
  | "failed";

export interface DebugBreakpoint {
  path: string;
  /** Where the user clicked. */
  line: number;
  condition: string;
  /** False means execution will not stop here — the adapter said so. */
  verified: boolean;
  /** Where the adapter actually put it, when it had to move it. */
  actual_line: number;
  moved: boolean;
  message: string;
}

export interface DebugFrame {
  id: number;
  name: string;
  path: string;
  line: number;
  column: number;
}

export interface DebugSessionInfo {
  id: string;
  adapter: string;
  state: DebugState;
  program: string;
  /** "breakpoint", "step", "exception", "entry". */
  stop_reason: string;
  thread_id: number;
  error: string;
  exit_code: number | null;
  output: string[];
  frames: DebugFrame[];
}

export interface DebugStatus {
  running: boolean;
  breakpoints: DebugBreakpoint[];
  session: DebugSessionInfo | null;
}

export interface DebugAdapterInfo {
  id: string;
  label: string;
  languages: string[];
  available: boolean;
  install: string;
}

export interface DebugScope {
  name: string;
  variables_reference: number;
  expensive: boolean;
}

export interface DebugVariable {
  name: string;
  value: string;
  type: string;
  /** Non-zero when it can be expanded. */
  variables_reference: number;
}

// ---- Tests (CODE ▸ Tests) ----

/**
 * "errored" is distinct from "failed" on purpose: a test whose setup blew up
 * did not test anything, and telling them apart is what stops a broken fixture
 * reading as a broken feature.
 */
export type TestStatus =
  | "passed"
  | "failed"
  | "errored"
  | "skipped"
  | "xfailed"
  | "xpassed";

export type TestRunStatus =
  | "pending"
  | "running"
  | "passed"
  | "failed"
  | "cancelled"
  | "errored";

export interface TestResult {
  /** The framework's own selector — the only thing that can re-run this test. */
  id: string;
  name: string;
  suite: string;
  file: string;
  line: number;
  status: TestStatus;
  duration_seconds: number;
  message: string;
  /** Where it broke, which is often not where the test is defined. */
  failure_file: string;
  failure_line: number;
}

export interface FileCoverage {
  path: string;
  covered: number;
  total: number;
  missing: number[];
}

export interface TestCoverage {
  source: string;
  covered: number;
  total: number;
  files: FileCoverage[];
}

export interface TestRun {
  id: string;
  framework: string;
  command: string;
  status: TestRunStatus;
  started_at: string;
  finished_at: string | null;
  duration_seconds: number;
  results: TestResult[];
  output: string;
  error: string;
  coverage: TestCoverage | null;
  selection: string[];
  counts: Record<TestStatus, number>;
}

export interface TestFramework {
  id: string;
  label: string;
  command: string;
  available: boolean;
  /** Why it cannot run, or the collection error that stopped discovery. */
  detail: string;
  /** -1 when discovery was not attempted for this framework. */
  test_count: number;
  supports_coverage: boolean;
}

export interface DiscoveredTest {
  id: string;
  name: string;
  suite: string;
  file: string;
  line: number;
}

export interface TestFrameworks {
  frameworks: TestFramework[];
  tests: DiscoveredTest[];
  running: boolean;
}

export interface TestLatest {
  running: boolean;
  run: TestRun | null;
}

// ---- Language intelligence (LSP) ----

export type DiagnosticSeverity = "error" | "warning" | "info" | "hint";

export interface Diagnostic {
  path: string;
  /** One-based, as the editor shows it. */
  line: number;
  column: number;
  end_line: number;
  end_column: number;
  severity: DiagnosticSeverity;
  message: string;
  source: string;
  code: string;
}

/**
 * Three outcomes, not two. `supported` false means no analyser exists for this
 * file type; `available` false means one exists but is not installed here.
 * Either way `diagnostics` is empty — and an empty list must never be rendered
 * as "no problems" unless `available` is true.
 */
export interface DiagnosticsResult {
  path: string;
  supported: boolean;
  available: boolean;
  diagnostics: Diagnostic[];
  detail: string;
}

export interface LanguageServerInfo {
  id: string;
  label: string;
  languages: string[];
  available: boolean;
  install: string;
}

export interface LanguageServers {
  servers: LanguageServerInfo[];
  running: { language: string; server: string; label: string }[];
}

export interface CodeLocation {
  path: string;
  line: number;
  column: number;
}

export interface LocationsResult {
  available: boolean;
  locations: CodeLocation[];
  detail: string;
  source?: "language-server" | "index";
}

export interface SymbolInfo {
  name: string;
  kind: string;
  path: string;
  line: number;
  signature: string | null;
}

export interface DocumentSymbols {
  available: boolean;
  symbols: SymbolInfo[];
  detail: string;
}

export interface WorkspaceSymbols {
  symbols: SymbolInfo[];
  /** "language-server" is semantic; "index" is text-derived and less precise. */
  source: "language-server" | "index";
  query: string;
}

export interface TextEdit {
  start_line: number;
  start_column: number;
  end_line: number;
  end_column: number;
  text: string;
}

export interface RenamePreview {
  available: boolean;
  edits: Record<string, TextEdit[]>;
  files?: number;
  count?: number;
  detail: string;
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
  /** One-based, so the editor can select the match rather than just scroll. */
  column?: number;
  length?: number;
  text: string;
  /** What this line would become, in a replace preview. */
  replacement?: string;
}

export interface SearchResult {
  query: string;
  matches: SearchMatch[];
  success: boolean;
  /** Set when the pattern itself is invalid — a malformed regex, usually. */
  error?: string;
  /** How many files held a match. */
  files?: number;
  /** True when the limit cut the results short, so the UI can say so. */
  truncated?: boolean;
  /** Binary or oversized files that were not searched. Counted, never hidden. */
  skipped?: number;
}

export interface SearchOptions {
  regex?: boolean;
  case_sensitive?: boolean;
  whole_word?: boolean;
  /** Comma-separated globs, e.g. "src/**,*.ts". */
  include?: string;
  exclude?: string;
  /** Present makes the request a preview: each match carries its new line. */
  replace?: string;
}

export interface ReplaceResult {
  files: string[];
  replacements: number;
  errors: string[];
}

/** One runnable command a project declares. */
export interface ProjectTask {
  id: string;
  label: string;
  command: string;
  /** Which file it came from: "npm", "make", "just", "compose", "user"… */
  source: string;
  cwd: string;
  detail: string;
  kind: "run" | "build" | "test" | "lint" | "other" | string;
}

export interface ProjectTasks {
  tasks: ProjectTask[];
  tasks_file: string;
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

export interface GitHunkLine {
  kind: "added" | "removed" | "context" | "marker";
  text: string;
}

export interface GitHunk {
  /** Position within this file's hunk list, for this reading of the diff. */
  index: number;
  header: string;
  /** The function or class Git names after the closing `@@`. */
  heading: string;
  old_start: number;
  new_start: number;
  added: number;
  removed: number;
  lines: GitHunkLine[];
}

export interface GitHunks {
  path: string;
  staged: boolean;
  binary: boolean;
  hunks: GitHunk[];
}

export interface GitBranch {
  name: string;
  upstream: string;
  current: boolean;
  ahead: number;
  behind: number;
  /** The upstream this tracked was deleted on the remote. */
  gone: boolean;
  commit: string;
  subject: string;
}

export interface GitBranches {
  repository: boolean;
  current?: string;
  branches: GitBranch[];
  remote_branches: string[];
  remotes: { name: string; url: string }[];
}

/** A merge, rebase or cherry-pick that has been started and not finished. */
export interface GitMergeState {
  merging: boolean;
  rebasing: boolean;
  cherry_picking: boolean;
  message: string;
  conflicts: string[];
}

export interface GitCommitContext extends Partial<GitMergeState> {
  repository: boolean;
  branch?: string;
  staged?: GitEntry[];
  can_amend?: boolean;
  previous_message?: string;
}

export interface GitConflictSides {
  path: string;
  /** null when the file did not exist on that side. */
  base: string | null;
  ours: string | null;
  theirs: string | null;
  /** The working-tree file, markers and all. */
  merged: string;
  language: string;
}

export interface GitSyncResult extends Partial<GitMergeState> {
  output: string;
  conflicted?: boolean;
  branches?: GitBranch[];
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
  frames: DesignFrame[];
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

export type PlanStatus = "proposed" | "approved" | "rejected" | "implemented";

export interface PlanStep {
  description: string;
  /** Paths the plan claimed it would touch — advisory, and reviewable. */
  paths: string[];
}

export interface DesignPlan {
  design_id: string;
  status: PlanStatus;
  summary: string;
  steps: PlanStep[];
  reviewed_paths: string[];
  questions: string[];
  rejection_reason: string;
  session_id: string;
  /** The design's version when the plan was written. */
  design_version: number;
  created_at: string;
  updated_at: string;
  implemented_at: string | null;
}

export interface DesignPlanStatus {
  plan: DesignPlan | null;
  /** Whether implementation is allowed right now — from the same gate the
   *  endpoint uses, so the button and the server cannot disagree. */
  can_implement: boolean;
  /** Why not, when not. */
  reason: string;
  design_version: number;
  /** The plan was written for an older version of the canvas. */
  stale: boolean;
}

/** The shapes a wireframe is made of. Mirrors `FrameElementType` on the server. */
export type FrameElementType =
  | "box"
  | "text"
  | "heading"
  | "button"
  | "input"
  | "image"
  | "list"
  | "nav";

/**
 * One element inside a mock-up frame.
 *
 * Coordinates are relative to the frame's own top-left corner, in the frame's
 * pixels, so a mock-up drawn at 1440x900 renders the same at any preview scale.
 * Unknown keys are preserved by the server, hence the index signature.
 */
export interface DesignFrameElement {
  id?: string;
  type: FrameElementType | string;
  label?: string;
  x?: number;
  y?: number;
  width?: number;
  height?: number;
  children?: DesignFrameElement[];
  [key: string]: unknown;
}

export interface DesignFrame {
  id: string;
  name: string;
  width: number;
  height: number;
  children: DesignFrameElement[];
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
  /** Refused outright by policy — no confirmation can start it. */
  refused?: boolean;
  /** Startable, but only after the user confirms the reasons below. */
  requires_approval?: boolean;
  approval_reasons?: string[];
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
  | "TaskSplit"
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
  | "WorkspaceUpdated"
  | "WorkspaceRunUpdated";

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

/** Which working tree a report was taken from. */
export interface CheckoutFingerprint {
  commit: string;
  branch: string;
  /** sha256 over the commit, the porcelain status, and the tracked diff. */
  digest: string;
  dirty: boolean;
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
  /** The checkout this verdict is about; empty digest means unpinnable. */
  checkout?: CheckoutFingerprint;
}

/** What the Inspector asks the backend to run. */
export interface RunInspectionRequest {
  profile: QAScanProfile;
  target_url: string;
  authorize_remote_target: boolean;
}

// ---- Change review (Inspector ▸ Review) ----

export type ReviewScope = "working" | "staged" | "branch" | "range";

export type ChangeKind = "added" | "modified" | "deleted" | "renamed" | "binary";

export interface ChangedFile {
  path: string;
  kind: ChangeKind;
  previous_path: string;
  insertions: number;
  deletions: number;
  binary: boolean;
  /** How many findings point at this file. */
  findings: number;
}

export interface ChangeReview {
  id: string;
  status: QAStatus;
  started_at: string;
  finished_at: string | null;
  project_root: string;
  scope: ReviewScope;
  base_ref: string;
  head_ref: string;
  subject: string;
  commits: string[];
  files: ChangedFile[];
  insertions: number;
  deletions: number;
  summary: string;
  intent: string;
  checks: QACheck[];
  specialists: QASpecialist[];
  findings: QAFinding[];
  verdict: QAVerdict;
  gate_reasons: string[];
  mission_id: string;
  /** The checkout the diff was taken from, so a stale review reads as stale. */
  checkout?: CheckoutFingerprint;
  /** Whether the stored patch was clipped at the archive ceiling. */
  patch_truncated?: boolean;
}

/** What a review of a scope would cover, resolved without running one. */
export interface ReviewSubject {
  scope: ReviewScope;
  base_ref: string;
  head_ref: string;
  label: string;
  commits: string[];
  files: number;
  untracked: string[];
  empty: boolean;
}

export interface ReviewLatest {
  running: boolean;
  review: ChangeReview | null;
  /** True when the reviewed checkout no longer matches the working tree. */
  stale?: boolean;
}

export interface ReviewHistory {
  running: boolean;
  reviews: ChangeReview[];
}

export interface RunReviewRequest {
  scope: ReviewScope;
  base_ref: string;
  head_ref: string;
}

export interface QALatest {
  running: boolean;
  report: QAReport | null;
  /**
   * True when the report's checkout no longer matches the working tree. A
   * verdict describes code, so once the code moves the verdict is history —
   * the tab badge has to stop claiming this checkout was cleared.
   */
  stale?: boolean;
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
  /** Steps that must finish before this one may run. */
  depends_on: string[];
  /** How many times the executor has tried this step. */
  attempts: number;
  /** Why the last attempt failed, when one did. */
  error: string;
  created_at: string;
  updated_at: string;
}

export type RunStatus =
  | "pending"
  | "running"
  | "paused"
  | "waiting_for_user"
  | "waiting_for_approval"
  | "completed"
  | "failed"
  | "cancelled";

export type RunStepKind =
  | "run_started"
  | "run_finished"
  | "task_started"
  | "task_completed"
  | "task_failed"
  | "task_skipped"
  | "artifact"
  | "source"
  | "note"
  | "steer"
  | "approval";

export interface RunStep {
  id: string;
  kind: RunStepKind;
  task_id: string;
  message: string;
  detail: Record<string, unknown>;
  created_at: string;
}

export interface PendingApproval {
  id: string;
  action: string;
  reason: string;
  level: string;
  requested_at: string;
}

/** One execution of a workspace's plan. */
export interface WorkspaceRun {
  id: string;
  workspace_id: string;
  goal: string;
  status: RunStatus;
  current_task_id: string;
  error: string;
  skill: string;
  profile: string;
  started_at: string | null;
  finished_at: string | null;
  total_tasks: number;
  completed_tasks: number;
  pending_approval: PendingApproval | null;
  steps: RunStep[];
  metadata: Record<string, unknown>;
  created_at: string;
  updated_at: string;
}

export type LinkTargetKind = "artifact" | "design" | "code" | "upload";
export type LinkRelation =
  | "derived_from"
  | "generated_from"
  | "depends_on"
  | "implements"
  | "describes"
  | "references";

/** One relationship: `source_path` was made from `target_path`. */
export interface ArtifactLink {
  id: string;
  source_path: string;
  source_kind: LinkTargetKind;
  target_path: string;
  target_kind: LinkTargetKind;
  relation: LinkRelation;
  title: string;
  target_revision: number;
  created_at: string;
}

/** A document written against something that has since changed. */
export interface StaleArtifact {
  link_id: string;
  path: string;
  source_of_truth: string;
  relation: LinkRelation;
  seen_revision: number;
  current_revision: number;
  reason: string;
}

export type ChangeAction = "created" | "updated" | "deleted";
export type ChangeStatus = "pending" | "accepted" | "rejected";
export type ChangeSetStatus = "open" | "accepted" | "rejected" | "partial";

export interface ChangeEntry {
  id: string;
  /** Workspace-relative path of the artifact that changed. */
  path: string;
  action: ChangeAction;
  /** The revision it had before this change; 0 means it did not exist. */
  before_version: number;
  after_version: number;
  status: ChangeStatus;
  summary: string;
}

/** Everything one agent operation changed, reviewed together. */
export interface ChangeSet {
  id: string;
  workspace_id: string;
  run_id: string;
  task_id: string;
  summary: string;
  status: ChangeSetStatus;
  entries: ChangeEntry[];
  created_at: string;
  updated_at: string;
}

export interface ChangeDiffLine {
  marker: string;
  number: number;
  text: string;
}

export interface ChangeDiff {
  path: string;
  change: string;
  lines: ChangeDiffLine[];
  added: number;
  removed: number;
  note: string;
}

export interface Skill {
  name: string;
  title: string;
  description: string;
  instructions: string;
  preferred_tools: string[];
  expected_artifacts: string[];
  checklist: string[];
  triggers: string[];
  kinds: string[];
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
  /**
   * sha256 of the file as it was read; only set by the single-document read.
   * Sent back on save so an edit written against a version the agent has since
   * replaced is refused instead of silently overwriting it.
   */
  digest?: string;
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

