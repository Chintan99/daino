// Small typed fetch wrapper for the Daino REST API.
// All URLs are relative ("/api/...") so the app works same-origin in production
// and through the Vite dev proxy in development.

import type {
  AgentConfig,
  ApprovalEntry,
  AuditLogPage,
  DocsIndex,
  DocsPage,
  EffectiveInstructions,
  CheckpointEntry,
  Design,
  DesignList,
  FileRead,
  FileTree,
  FileWriteResult,
  ExecutionPrompt,
  ExecutionTrace,
  GitDiff,
  GitFileDiff,
  GitStatus,
  Health,
  MemoryItem,
  MissionDetails,
  MissionSummary,
  PreviewCommand,
  PreviewDetect,
  CatalogModel,
  PreviewStatus,
  ProjectSettings,
  ProviderForm,
  ProviderHealth,
  ProviderSaveResult,
  ProviderTestResult,
  QAHistory,
  QALatest,
  QAScanProfile,
  RepositoryInfo,
  ArtifactLink,
  ChangeDiff,
  ChangeSet,
  StaleArtifact,
  ReviewHistory,
  Skill,
  WorkspaceRun,
  ReviewLatest,
  ReviewScope,
  ReviewSubject,
  RunInspectionRequest,
  RunReviewRequest,
  SearchResult,
  SessionList,
  SessionMessages,
  SettingsPatch,
  TerminalCreated,
  TerminalList,
  Workspace,
  WorkspaceSummary,
  WorkspaceTask,
  WorkspaceTemplate,
  Artifact,
  ArtifactContent,
  ArtifactRevision,
  CreateWorkspaceRequest,
  ProjectInfo,
  ResearchSource,
  Diagnostic,
  DiagnosticsResult,
  DocumentSymbols,
  LanguageServers,
  LocationsResult,
  RenamePreview,
  TextEdit,
  WorkspaceSymbols,
  TestFrameworks,
  TestLatest,
  GitBranches,
  GitCommitContext,
  GitConflictSides,
  GitHunks,
  GitMergeState,
  GitSyncResult,
  ProjectTask,
  ProjectTasks,
  ReplaceResult,
  SearchOptions,
  DesignPlanStatus,
  DebugAdapterInfo,
  DebugScope,
  DebugStatus,
  DebugVariable,
} from "./types";

export class ApiError extends Error {
  status: number;
  detail: unknown;
  constructor(status: number, message: string, detail: unknown) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }
}

async function request<T>(
  method: string,
  path: string,
  body?: unknown,
): Promise<T> {
  const init: RequestInit = { method, headers: {} };
  if (body !== undefined) {
    (init.headers as Record<string, string>)["Content-Type"] =
      "application/json";
    init.body = JSON.stringify(body);
  }
  const res = await fetch(path, init);
  const text = await res.text();
  let parsed: unknown = undefined;
  if (text) {
    try {
      parsed = JSON.parse(text);
    } catch {
      parsed = text;
    }
  }
  if (!res.ok) {
    const detail =
      parsed && typeof parsed === "object" && "detail" in parsed
        ? (parsed as { detail: unknown }).detail
        : parsed;
    const message =
      typeof detail === "object" && detail && "message" in detail
        ? String((detail as { message: unknown }).message)
        : typeof detail === "string"
          ? detail
          : `${method} ${path} failed (${res.status})`;
    throw new ApiError(res.status, message, detail);
  }
  return parsed as T;
}

function qs(params: Record<string, string | number | boolean | undefined>): string {
  const parts: string[] = [];
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === "") continue;
    parts.push(`${encodeURIComponent(key)}=${encodeURIComponent(String(value))}`);
  }
  return parts.length ? `?${parts.join("&")}` : "";
}

export const api = {
  // Health & workspace
  health: () => request<Health>("GET", "/api/health"),
  projectInfo: () => request<ProjectInfo>("GET", "/api/workspace"),

  // Sessions
  listSessions: () => request<SessionList>("GET", "/api/sessions"),
  /** An empty title leaves the session unnamed; its first request names it. */
  createSession: (title = "") =>
    request<{ id: string; title: string }>("POST", "/api/sessions", { title }),
  latestSession: () => request<{ id: string }>("GET", "/api/sessions/latest"),
  sessionMessages: (id: string) =>
    request<SessionMessages>("GET", `/api/sessions/${encodeURIComponent(id)}/messages`),
  sessionTodos: (id: string) =>
    request<{ session_id: string; todos: unknown[] }>(
      "GET",
      `/api/sessions/${encodeURIComponent(id)}/todos`,
    ),
  sessionContext: (id: string) =>
    request<{ session_id: string; files: string[] }>(
      "GET",
      `/api/sessions/${encodeURIComponent(id)}/context`,
    ),
  selectSessionModel: (id: string, profile: string) =>
    request<{ session_id: string; profile: string }>(
      "POST",
      `/api/sessions/${encodeURIComponent(id)}/model`,
      { profile },
    ),
  attachContext: (id: string, path: string) =>
    request<{ path: string; attached: boolean }>(
      "POST",
      `/api/sessions/${encodeURIComponent(id)}/context`,
      { path },
    ),

  // Files
  fileTree: (path: string) =>
    request<FileTree>("GET", `/api/files/tree${qs({ path })}`),
  readFile: (path: string) =>
    request<FileRead>("GET", `/api/files/read${qs({ path })}`),
  writeFile: (path: string, content: string, base_hash: string) =>
    request<FileWriteResult>("PUT", "/api/files/write", {
      path,
      content,
      base_hash,
    }),
  createFile: (path: string, is_dir: boolean) =>
    request<{ path: string; type: string }>("POST", "/api/files/create", {
      path,
      is_dir,
    }),
  renameFile: (source: string, dest: string) =>
    request<{ source: string; dest: string }>("POST", "/api/files/rename", {
      source,
      dest,
    }),
  deleteFile: (path: string) =>
    request<{ path: string; deleted: boolean }>(
      "DELETE",
      `/api/files/delete${qs({ path })}`,
    ),
  attachFile: (name: string, contentBase64: string) =>
    request<{ path: string; name: string; bytes: number }>(
      "POST",
      "/api/files/attach",
      { name, content_base64: contentBase64 },
    ),
  search: (q: string, regex = false, limit = 200) =>
    request<SearchResult>("GET", `/api/files/search${qs({ q, regex, limit })}`),

  // Git
  gitStatus: () => request<GitStatus>("GET", "/api/git/status"),
  gitDiff: (path: string, staged = false) =>
    request<GitDiff>("GET", `/api/git/diff${qs({ path, staged })}`),
  gitLog: (limit = 50) =>
    request<{ repository: boolean; entries: string[] }>(
      "GET",
      `/api/git/log${qs({ limit })}`,
    ),

  // Designs
  listDesigns: () => request<DesignList>("GET", "/api/designs"),
  createDesign: (name: string, type: string) =>
    request<Design>("POST", "/api/designs", { name, type }),
  generateDesignFromCode: () =>
    request<Design>("POST", "/api/designs/generate-from-code", {}),
  getDesign: (id: string) =>
    request<Design>("GET", `/api/designs/${encodeURIComponent(id)}`),
  saveDesign: (id: string, design: Design) =>
    request<Design>("PUT", `/api/designs/${encodeURIComponent(id)}`, design),
  deleteDesign: (id: string) =>
    request<{ id: string; deleted: boolean }>(
      "DELETE",
      `/api/designs/${encodeURIComponent(id)}`,
    ),
  addNode: (
    id: string,
    body: {
      label: string;
      node_type: string;
      node_id?: string;
      x: number;
      y: number;
      data?: Record<string, unknown>;
    },
  ) =>
    request<Design>(
      "POST",
      `/api/designs/${encodeURIComponent(id)}/nodes`,
      body,
    ),
  patchNode: (
    id: string,
    nodeId: string,
    body: {
      label?: string;
      node_type?: string;
      x?: number;
      y?: number;
      data?: Record<string, unknown>;
    },
  ) =>
    request<Design>(
      "PATCH",
      `/api/designs/${encodeURIComponent(id)}/nodes/${encodeURIComponent(nodeId)}`,
      body,
    ),
  deleteNode: (id: string, nodeId: string) =>
    request<Design>(
      "DELETE",
      `/api/designs/${encodeURIComponent(id)}/nodes/${encodeURIComponent(nodeId)}`,
    ),
  addEdge: (id: string, body: { source: string; target: string; label?: string }) =>
    request<Design>(
      "POST",
      `/api/designs/${encodeURIComponent(id)}/edges`,
      body,
    ),
  deleteEdge: (id: string, edgeId: string) =>
    request<Design>(
      "DELETE",
      `/api/designs/${encodeURIComponent(id)}/edges/${encodeURIComponent(edgeId)}`,
    ),

  gitFile: (path: string, staged = false) =>
    request<GitFileDiff>("GET", `/api/git/file${qs({ path, staged })}`),
  gitStage: (paths: string[]) =>
    request<{ staged: string[] }>("POST", "/api/git/stage", { paths }),
  gitUnstage: (paths: string[]) =>
    request<{ unstaged: string[] }>("POST", "/api/git/unstage", { paths }),
  gitDiscard: (paths: string[]) =>
    request<{ discarded: string[] }>("POST", "/api/git/discard", { paths }),

  // Engineering evidence (the browser side of the TUI workspace views)
  logs: (q: string, limit = 500) =>
    request<AuditLogPage>("GET", `/api/logs${qs({ q, limit })}`),
  mapPrompts: (limit = 100) =>
    request<{ prompts: ExecutionPrompt[] }>(
      "GET",
      `/api/map/prompts${qs({ limit })}`,
    ),
  mapTrace: (missionId: string) =>
    request<ExecutionTrace>(
      "GET",
      `/api/map/prompts/${encodeURIComponent(missionId)}`,
    ),
  qaLatest: () => request<QALatest>("GET", "/api/qa/latest"),
  qaHistory: (limit = 50) =>
    request<QAHistory>("GET", `/api/qa/history${qs({ limit })}`),
  qaReport: (id: string) =>
    request<QALatest>("GET", `/api/qa/reports/${encodeURIComponent(id)}`),
  qaRun: (options: Partial<RunInspectionRequest> = {}) =>
    request<{ running: boolean; profile: QAScanProfile; target_url: string }>(
      "POST",
      "/api/qa/run",
      {
        profile: options.profile ?? "full",
        target_url: options.target_url ?? "",
        authorize_remote_target: options.authorize_remote_target ?? false,
      },
    ),
  qaCancel: () =>
    request<{ cancelled: boolean }>("POST", "/api/qa/cancel", {}),
  missions: (limit = 100) =>
    request<{ missions: MissionSummary[] }>("GET", `/api/missions${qs({ limit })}`),
  missionDetails: (id: string) =>
    request<MissionDetails>("GET", `/api/missions/${encodeURIComponent(id)}`),
  checkpoints: (missionId = "") =>
    request<{ checkpoints: CheckpointEntry[] }>(
      "GET",
      `/api/checkpoints${qs({ mission_id: missionId })}`,
    ),
  approvals: (limit = 100) =>
    request<{ approvals: ApprovalEntry[] }>(
      "GET",
      `/api/approvals${qs({ limit })}`,
    ),
  repository: () => request<RepositoryInfo>("GET", "/api/repository"),

  // Documentation (served at /docs, separate from the /api-docs reference)
  docsIndex: () => request<DocsIndex>("GET", "/api/docs"),
  docsPage: (slug: string) =>
    request<DocsPage>("GET", `/api/docs/${encodeURIComponent(slug)}`),
  reindex: () =>
    request<{ file_count: number; frameworks: string[] }>(
      "POST",
      "/api/repository/index",
      {},
    ),

  // Design plans: nothing is written until a plan is approved.
  designPlan: (id: string) =>
    request<DesignPlanStatus>("GET", `/api/designs/${encodeURIComponent(id)}/plan`),
  proposeDesignPlan: (id: string, sessionId: string, profile = "") =>
    request<DesignPlanStatus>("POST", `/api/designs/${encodeURIComponent(id)}/plan`, {
      session_id: sessionId,
      profile,
    }),
  approveDesignPlan: (id: string) =>
    request<DesignPlanStatus>(
      "POST",
      `/api/designs/${encodeURIComponent(id)}/plan/approve`,
      {},
    ),
  rejectDesignPlan: (id: string, reason = "") =>
    request<DesignPlanStatus>(
      "POST",
      `/api/designs/${encodeURIComponent(id)}/plan/reject`,
      { reason },
    ),
  implementDesign: (id: string, sessionId: string, profile = "") =>
    request<DesignPlanStatus & { implemented: boolean; summary: string; files: string[] }>(
      "POST",
      `/api/designs/${encodeURIComponent(id)}/implement`,
      { session_id: sessionId, profile },
    ),

  // Design frames (UI mock-up viewports).
  addFrame: (
    id: string,
    body: { name?: string; width?: number; height?: number; children?: unknown[] },
  ) =>
    request<Design>("POST", `/api/designs/${encodeURIComponent(id)}/frames`, body),
  updateFrame: (
    id: string,
    frameId: string,
    body: { name?: string; width?: number; height?: number; children?: unknown[] },
  ) =>
    request<Design>(
      "PATCH",
      `/api/designs/${encodeURIComponent(id)}/frames/${encodeURIComponent(frameId)}`,
      body,
    ),
  deleteFrame: (id: string, frameId: string) =>
    request<Design>(
      "DELETE",
      `/api/designs/${encodeURIComponent(id)}/frames/${encodeURIComponent(frameId)}`,
    ),

  // Search and replace across the repository.
  searchFiles: (q: string, options: SearchOptions = {}, limit = 500) =>
    request<SearchResult>(
      "GET",
      `/api/files/search${qs({
        q,
        limit,
        regex: options.regex ?? false,
        case_sensitive: options.case_sensitive ?? false,
        whole_word: options.whole_word ?? false,
        include: options.include || undefined,
        exclude: options.exclude || undefined,
        replace: options.replace,
      })}`,
    ),
  replaceInFiles: (
    query: string,
    replacement: string,
    options: SearchOptions & { paths?: string[] } = {},
  ) =>
    request<ReplaceResult>("POST", "/api/files/replace", {
      query,
      replacement,
      regex: options.regex ?? false,
      case_sensitive: options.case_sensitive ?? false,
      whole_word: options.whole_word ?? false,
      include: options.include ?? "",
      exclude: options.exclude ?? "",
      paths: options.paths ?? [],
    }),

  // Run configurations and tasks.
  projectTasks: () => request<ProjectTasks>("GET", "/api/tasks"),
  saveProjectTasks: (tasks: Partial<ProjectTask>[]) =>
    request<ProjectTasks>("PUT", "/api/tasks", { tasks }),

  // Git: the parts beyond review and staging.
  gitHunks: (path: string, staged = false) =>
    request<GitHunks>("GET", `/api/git/hunks${qs({ path, staged })}`),
  gitStageHunks: (path: string, hunks: number[]) =>
    request<{ staged: string; hunks: number[] }>("POST", "/api/git/stage-hunks", {
      path,
      hunks,
    }),
  gitUnstageHunks: (path: string, hunks: number[]) =>
    request<{ unstaged: string; hunks: number[] }>(
      "POST",
      "/api/git/unstage-hunks",
      { path, hunks },
    ),
  gitCommitContext: () =>
    request<GitCommitContext>("GET", "/api/git/commit-context"),
  gitCommit: (message: string, options: { amend?: boolean; sign_off?: boolean } = {}) =>
    request<{ committed: boolean; revision: string; output: string }>(
      "POST",
      "/api/git/commit",
      { message, amend: options.amend ?? false, sign_off: options.sign_off ?? false },
    ),
  gitBranches: () => request<GitBranches>("GET", "/api/git/branches"),
  gitSwitchBranch: (name: string, options: { create?: boolean; start?: string } = {}) =>
    request<{ branch: string; created: boolean; output: string }>(
      "POST",
      "/api/git/branch",
      { name, create: options.create ?? false, start: options.start ?? "" },
    ),
  gitDeleteBranch: (name: string, force = false) =>
    request<{ deleted: string; output: string }>(
      "DELETE",
      `/api/git/branch${qs({ name, force })}`,
    ),
  gitFetch: (remote = "") =>
    request<GitSyncResult>("POST", "/api/git/fetch", { remote }),
  gitPull: (rebase = false) =>
    request<GitSyncResult>("POST", "/api/git/pull", { rebase }),
  gitPush: (options: { remote?: string; branch?: string; set_upstream?: boolean } = {}) =>
    request<GitSyncResult>("POST", "/api/git/push", {
      remote: options.remote ?? "",
      branch: options.branch ?? "",
      set_upstream: options.set_upstream ?? false,
    }),
  gitMerge: (ref: string, noCommit = false) =>
    request<GitSyncResult>("POST", "/api/git/merge", { ref, no_commit: noCommit }),
  gitAbortMerge: () =>
    request<GitMergeState & { aborted: boolean }>("POST", "/api/git/merge/abort", {}),
  gitConflicts: () =>
    request<GitMergeState & { repository: boolean }>("GET", "/api/git/conflicts"),
  gitConflictSides: (path: string) =>
    request<GitConflictSides>("GET", `/api/git/conflict${qs({ path })}`),
  gitResolveConflict: (path: string, side: "ours" | "theirs") =>
    request<GitMergeState & { resolved: string }>(
      "POST",
      "/api/git/conflict/resolve",
      { path, side },
    ),
  gitMarkResolved: (paths: string[]) =>
    request<GitMergeState & { resolved: string[] }>(
      "POST",
      "/api/git/conflict/mark-resolved",
      { paths },
    ),

  // Debugging (CODE ▸ Debug)
  debugAdapters: () =>
    request<{ adapters: DebugAdapterInfo[] }>("GET", "/api/debug/adapters"),
  debugState: () => request<DebugStatus>("GET", "/api/debug/state"),
  toggleBreakpoint: (path: string, line: number) =>
    request<DebugStatus>("POST", "/api/debug/breakpoints/toggle", { path, line }),
  setBreakpointCondition: (path: string, line: number, condition: string) =>
    request<DebugStatus>("POST", "/api/debug/breakpoints/condition", {
      path,
      line,
      condition,
    }),
  clearBreakpoints: (path = "") =>
    request<DebugStatus>(
      "DELETE",
      `/api/debug/breakpoints${qs({ path: path || undefined })}`,
    ),
  debugLaunch: (body: {
    program?: string;
    module?: string;
    args?: string[];
    stop_on_entry?: boolean;
  }) =>
    request<DebugStatus>("POST", "/api/debug/launch", {
      program: body.program ?? "",
      module: body.module ?? "",
      args: body.args ?? [],
      stop_on_entry: body.stop_on_entry ?? false,
    }),
  debugControl: (
    command: "continue" | "pause" | "step-over" | "step-into" | "step-out" | "stop",
  ) => request<DebugStatus>("POST", `/api/debug/${command}`, {}),
  debugStack: () => request<DebugStatus>("GET", "/api/debug/stack"),
  debugScopes: (frameId: number) =>
    request<{ scopes: DebugScope[] }>("GET", `/api/debug/scopes${qs({ frame_id: frameId })}`),
  debugVariables: (reference: number) =>
    request<{ variables: DebugVariable[] }>(
      "GET",
      `/api/debug/variables${qs({ reference })}`,
    ),
  debugEvaluate: (expression: string, frameId = 0) =>
    request<{ result: string; type: string; variables_reference: number }>(
      "POST",
      "/api/debug/evaluate",
      { expression, frame_id: frameId },
    ),

  // Tests (CODE ▸ Tests)
  testFrameworks: (framework = "") =>
    request<TestFrameworks>(
      "GET",
      `/api/tests/frameworks${qs({ framework: framework || undefined })}`,
    ),
  testLatest: () => request<TestLatest>("GET", "/api/tests/latest"),
  runTests: (options: {
    framework?: string;
    selection?: string[];
    coverage?: boolean;
    failed_only?: boolean;
  } = {}) =>
    request<TestLatest>("POST", "/api/tests/run", {
      framework: options.framework ?? "",
      selection: options.selection ?? [],
      coverage: options.coverage ?? false,
      failed_only: options.failed_only ?? false,
    }),
  cancelTests: () =>
    request<{ cancelled: boolean }>("POST", "/api/tests/cancel", {}),

  // Language intelligence (CODE ▸ Problems, Go to definition, Find references)
  languageServers: () => request<LanguageServers>("GET", "/api/lsp/servers"),
  // The buffer's text rides along so diagnostics describe what is on screen
  // rather than what was last saved.
  diagnostics: (path: string, text?: string) =>
    request<DiagnosticsResult>("POST", "/api/lsp/diagnostics", { path, text }),
  closeDocument: (path: string) =>
    request<{ closed: string }>("POST", "/api/lsp/close", { path }),
  definition: (path: string, line: number, column: number) =>
    request<LocationsResult>("POST", "/api/lsp/definition", { path, line, column }),
  references: (path: string, line: number, column: number) =>
    request<LocationsResult>("POST", "/api/lsp/references", { path, line, column }),
  implementations: (path: string, line: number, column: number) =>
    request<LocationsResult>("POST", "/api/lsp/implementations", {
      path,
      line,
      column,
    }),
  hoverInfo: (path: string, line: number, column: number) =>
    request<{ available: boolean; markdown: string; detail: string }>(
      "POST",
      "/api/lsp/hover",
      { path, line, column },
    ),
  documentSymbols: (path: string) =>
    request<DocumentSymbols>("GET", `/api/lsp/symbols${qs({ path })}`),
  workspaceSymbols: (query: string, limit = 200) =>
    request<WorkspaceSymbols>(
      "GET",
      `/api/lsp/workspace-symbols${qs({ query, limit })}`,
    ),
  previewRename: (path: string, line: number, column: number, newName: string) =>
    request<RenamePreview>("POST", "/api/lsp/rename", {
      path,
      line,
      column,
      new_name: newName,
    }),
  applyRename: (edits: Record<string, TextEdit[]>) =>
    request<{ written: string[] }>("POST", "/api/lsp/rename/apply", { edits }),

  // Change review (Inspector ▸ Review)
  reviewSubject: (scope: ReviewScope, baseRef = "") =>
    request<ReviewSubject>(
      "GET",
      `/api/review/subject${qs({ scope, base_ref: baseRef || undefined })}`,
    ),
  reviewLatest: () => request<ReviewLatest>("GET", "/api/review/latest"),
  reviewHistory: (limit = 50) =>
    request<ReviewHistory>("GET", `/api/review/history${qs({ limit })}`),
  reviewReport: (id: string) =>
    request<ReviewLatest>("GET", `/api/review/reports/${encodeURIComponent(id)}`),
  // `reviewId` is what keeps a saved review honest: the server then serves the
  // patch that review recorded instead of re-deriving one from today's working
  // tree, so old findings are never rendered beside code written since.
  reviewFileDiff: (
    path: string,
    scope: ReviewScope,
    baseRef = "",
    reviewId = "",
  ) =>
    request<{
      path: string;
      patch: string;
      readable: boolean;
      archived?: boolean;
      detail?: string;
    }>(
      "GET",
      `/api/review/diff${qs({
        path,
        scope,
        base_ref: baseRef || undefined,
        review_id: reviewId || undefined,
      })}`,
    ),
  reviewRun: (options: Partial<RunReviewRequest> = {}) =>
    request<{ running: boolean; scope: ReviewScope; subject: string }>(
      "POST",
      "/api/review/run",
      {
        scope: options.scope ?? "working",
        base_ref: options.base_ref ?? "",
        head_ref: options.head_ref ?? "",
      },
    ),
  reviewCancel: () =>
    request<{ cancelled: boolean }>("POST", "/api/review/cancel", {}),

  // Preview
  previewDetect: () => request<PreviewDetect>("GET", "/api/preview/detect"),
  previewStatus: () => request<PreviewStatus>("GET", "/api/preview/status"),
  previewStart: (command: string, url: string, confirm = false) =>
    request<{ running: boolean; command: string; url: string }>(
      "POST",
      "/api/preview/start",
      { command, url, confirm },
    ),
  previewStop: () =>
    request<{ running: boolean }>("POST", "/api/preview/stop", {}),

  // Agent customization: autonomy, effort, instructions, memory, playbooks
  agentConfig: (sessionId: string) =>
    request<AgentConfig>("GET", `/api/agent/config${qs({ session_id: sessionId })}`),
  setAutonomy: (sessionId: string, mode: string) =>
    request<{ mode: string; hint: string }>("POST", "/api/agent/autonomy", {
      session_id: sessionId,
      mode,
    }),
  setEffort: (sessionId: string, effort: string) =>
    request<{ profile: string; effort: string }>("POST", "/api/agent/effort", {
      session_id: sessionId,
      effort,
    }),
  setVerbose: (sessionId: string, enabled: boolean) =>
    request<{ verbose: boolean }>("POST", "/api/agent/verbose", {
      session_id: sessionId,
      enabled,
    }),
  globalInstructions: () =>
    request<{ path: string; exists: boolean; content: string }>(
      "GET",
      "/api/agent/instructions/global",
    ),
  saveGlobalInstructions: (content: string) =>
    request<{ path: string; bytes: number }>("PUT", "/api/agent/instructions/global", {
      content,
    }),
  effectiveInstructions: (path: string) =>
    request<EffectiveInstructions>(
      "GET",
      `/api/agent/instructions/effective${qs({ path })}`,
    ),
  listMemory: (params: { q?: string; type?: string; scope?: string; limit?: number }) =>
    request<{ items: MemoryItem[] }>("GET", `/api/agent/memory${qs({ ...params })}`),
  remember: (body: { content: string; summary?: string; tags?: string[]; scope?: string }) =>
    request<{ id: string }>("POST", "/api/agent/memory", body),
  forgetMemory: (id: string) =>
    request<{ forgotten: boolean }>("DELETE", `/api/agent/memory/${encodeURIComponent(id)}`),
  verifyMemory: (id: string) =>
    request<{ verified: boolean }>(
      "POST",
      `/api/agent/memory/${encodeURIComponent(id)}/verify`,
      {},
    ),
  clearMemory: (scope: "session" | "project", sessionId = "") =>
    request<{ cleared: number }>("POST", "/api/agent/memory/clear", {
      scope,
      session_id: sessionId,
    }),

  // Settings (project/agent configuration)
  settings: () => request<ProjectSettings>("GET", "/api/settings"),
  patchSettings: (body: SettingsPatch) =>
    request<ProjectSettings>("PATCH", "/api/settings", body),
  reloadSettings: () =>
    request<ProjectSettings>("POST", "/api/settings/reload", {}),
  providerHealth: () =>
    request<{ providers: ProviderHealth[] }>("GET", "/api/settings/providers/health"),
  saveProvider: (body: ProviderForm) =>
    request<ProviderSaveResult>("POST", "/api/settings/providers", body),
  testProvider: (body: ProviderForm) =>
    request<ProviderTestResult>("POST", "/api/settings/providers/test", body),
  providerCatalog: (body: ProviderForm) =>
    request<{ models: CatalogModel[] }>("POST", "/api/settings/providers/catalog", body),

  // Workspaces (the WORKSPACE tab)
  //
  // Artifact paths travel as query parameters rather than path segments: a
  // nested document needs no encoding gymnastics, and a traversal attempt
  // reaches the server's containment check as data rather than as a URL.
  workspaces: (includeArchived = false) =>
    request<{ workspaces: WorkspaceSummary[] }>(
      "GET",
      `/api/workspaces${qs({ include_archived: includeArchived || undefined })}`,
    ),
  workspace: (id: string) =>
    request<Workspace>("GET", `/api/workspaces/${encodeURIComponent(id)}`),
  createWorkspace: (body: Partial<CreateWorkspaceRequest>) =>
    request<Workspace>("POST", "/api/workspaces", {
      name: body.name ?? "Untitled workspace",
      goal: body.goal ?? "",
      kind: body.kind ?? "general",
      folder: body.folder ?? "",
    }),
  updateWorkspace: (
    id: string,
    body: Partial<Pick<Workspace, "name" | "goal" | "kind" | "status">>,
  ) => request<Workspace>("PATCH", `/api/workspaces/${encodeURIComponent(id)}`, body),
  deleteWorkspace: (id: string, removeFiles = false) =>
    request<{ deleted: string; files_removed: boolean }>(
      "DELETE",
      `/api/workspaces/${encodeURIComponent(id)}${qs({
        remove_files: removeFiles || undefined,
      })}`,
    ),
  workspaceTemplates: () =>
    request<{ templates: WorkspaceTemplate[] }>("GET", "/api/workspaces/templates"),
  attachWorkspaceSession: (id: string, sessionId: string) =>
    request<{ workspace_id: string; session_id: string }>(
      "POST",
      `/api/workspaces/${encodeURIComponent(id)}/session`,
      { session_id: sessionId },
    ),

  readArtifact: (id: string, path: string) =>
    request<ArtifactContent>(
      "GET",
      `/api/workspaces/${encodeURIComponent(id)}/artifact${qs({ path })}`,
    ),
  // `baseDigest` is optimistic concurrency: the digest the draft was read at.
  // The server refuses the write when the file has moved on since — an agent
  // finishing a step used to silently lose to whatever the editor held.
  writeArtifact: (
    id: string,
    path: string,
    content: string,
    baseDigest = "",
  ) =>
    request<Artifact>("PUT", `/api/workspaces/${encodeURIComponent(id)}/artifact`, {
      path,
      content,
      author: "user",
      base_digest: baseDigest,
    }),
  deleteArtifact: (id: string, path: string) =>
    request<{ deleted: string }>(
      "DELETE",
      `/api/workspaces/${encodeURIComponent(id)}/artifact${qs({ path })}`,
    ),
  artifactRevisions: (id: string, path: string) =>
    request<{ path: string; revisions: ArtifactRevision[] }>(
      "GET",
      `/api/workspaces/${encodeURIComponent(id)}/revisions${qs({ path })}`,
    ),
  artifactRevision: (id: string, path: string, version: number) =>
    request<{ path: string; version: number; content: string }>(
      "GET",
      `/api/workspaces/${encodeURIComponent(id)}/revision${qs({ path, version })}`,
    ),
  restoreArtifactRevision: (id: string, path: string, version: number) =>
    request<Artifact>(
      "POST",
      `/api/workspaces/${encodeURIComponent(id)}/revision/restore${qs({ path, version })}`,
      {},
    ),

  setWorkspaceTasks: (id: string, tasks: string[]) =>
    request<{ tasks: WorkspaceTask[] }>(
      "PUT",
      `/api/workspaces/${encodeURIComponent(id)}/tasks`,
      { tasks },
    ),
  addWorkspaceTask: (id: string, content: string) =>
    request<WorkspaceTask>("POST", `/api/workspaces/${encodeURIComponent(id)}/tasks`, {
      content,
    }),
  updateWorkspaceTask: (
    id: string,
    taskId: string,
    body: Partial<Pick<WorkspaceTask, "content" | "status" | "notes" | "artifact_path">>,
  ) =>
    request<WorkspaceTask>(
      "PATCH",
      `/api/workspaces/${encodeURIComponent(id)}/tasks/${encodeURIComponent(taskId)}`,
      body,
    ),
  reorderWorkspaceTasks: (id: string, taskIds: string[]) =>
    request<{ tasks: WorkspaceTask[] }>(
      "POST",
      `/api/workspaces/${encodeURIComponent(id)}/tasks/reorder`,
      { task_ids: taskIds },
    ),
  deleteWorkspaceTask: (id: string, taskId: string) =>
    request<{ deleted: string }>(
      "DELETE",
      `/api/workspaces/${encodeURIComponent(id)}/tasks/${encodeURIComponent(taskId)}`,
    ),

  // Runs: executing the plan rather than only recording it.
  workspaceRun: (id: string) =>
    request<{ run: WorkspaceRun | null }>(
      "GET",
      `/api/workspaces/${encodeURIComponent(id)}/run`,
    ),
  startWorkspaceRun: (
    id: string,
    body: { goal?: string; profile?: string; skill?: string } = {},
  ) =>
    request<{ run: WorkspaceRun }>("POST", `/api/workspaces/${encodeURIComponent(id)}/run`, {
      goal: body.goal ?? "",
      profile: body.profile ?? "",
      skill: body.skill ?? "",
    }),
  pauseWorkspaceRun: (runId: string) =>
    request<{ run: WorkspaceRun }>(
      "POST",
      `/api/workspaces/runs/${encodeURIComponent(runId)}/pause`,
      {},
    ),
  resumeWorkspaceRun: (runId: string) =>
    request<{ run: WorkspaceRun }>(
      "POST",
      `/api/workspaces/runs/${encodeURIComponent(runId)}/resume`,
      {},
    ),
  stopWorkspaceRun: (runId: string) =>
    request<{ run: WorkspaceRun }>(
      "POST",
      `/api/workspaces/runs/${encodeURIComponent(runId)}/stop`,
      {},
    ),
  steerWorkspaceRun: (runId: string, instruction: string) =>
    request<{ run: WorkspaceRun }>(
      "POST",
      `/api/workspaces/runs/${encodeURIComponent(runId)}/steer`,
      { instruction },
    ),
  resolveRunApproval: (runId: string, approvalId: string, approved: boolean) =>
    request<{ run: WorkspaceRun }>(
      "POST",
      `/api/workspaces/runs/${encodeURIComponent(runId)}/approval`,
      { approval_id: approvalId, approved },
    ),
  retryRunTask: (runId: string, taskId: string) =>
    request<{ run: WorkspaceRun }>(
      "POST",
      `/api/workspaces/runs/${encodeURIComponent(runId)}/tasks/${encodeURIComponent(taskId)}/retry`,
      {},
    ),
  skipRunTask: (runId: string, taskId: string) =>
    request<{ run: WorkspaceRun }>(
      "POST",
      `/api/workspaces/runs/${encodeURIComponent(runId)}/tasks/${encodeURIComponent(taskId)}/skip`,
      {},
    ),
  // Provenance: what came from what, and what has fallen behind.
  workspaceLinks: (id: string) =>
    request<{ links: ArtifactLink[]; stale: StaleArtifact[] }>(
      "GET",
      `/api/workspaces/${encodeURIComponent(id)}/links`,
    ),
  linkWorkspaceArtifacts: (
    id: string,
    body: { source_path: string; target_path: string; relation?: string; title?: string },
  ) => request<ArtifactLink>("POST", `/api/workspaces/${encodeURIComponent(id)}/links`, body),
  acknowledgeWorkspaceLink: (id: string, linkId: string) =>
    request<{ acknowledged: string }>(
      "POST",
      `/api/workspaces/${encodeURIComponent(id)}/links/${encodeURIComponent(linkId)}/acknowledge`,
      {},
    ),
  createWorkspaceDeliverable: (
    id: string,
    body: { path: string; format: string; title?: string },
  ) =>
    request<Artifact>("POST", `/api/workspaces/${encodeURIComponent(id)}/deliverable`, {
      path: body.path,
      format: body.format,
      title: body.title ?? "",
    }),

  // Change sets: the group of artifacts one step touched.
  workspaceChanges: (id: string, runId = "") =>
    request<{ changes: ChangeSet[] }>(
      "GET",
      `/api/workspaces/${encodeURIComponent(id)}/changes${qs({ run_id: runId || undefined })}`,
    ),
  workspaceChangeDiff: (id: string, changeSetId: string, path: string) =>
    request<ChangeDiff>(
      "GET",
      `/api/workspaces/${encodeURIComponent(id)}/changes/${encodeURIComponent(
        changeSetId,
      )}/diff${qs({ path })}`,
    ),
  decideWorkspaceChange: (
    id: string,
    changeSetId: string,
    body: { accepted: boolean; path?: string },
  ) =>
    request<ChangeSet>(
      "POST",
      `/api/workspaces/${encodeURIComponent(id)}/changes/${encodeURIComponent(
        changeSetId,
      )}/decide`,
      { accepted: body.accepted, path: body.path ?? "" },
    ),

  workspaceSkills: () =>
    request<{ skills: Skill[] }>("GET", "/api/workspaces/meta/skills"),

  workspaceSources: (id: string) =>
    request<{ sources: ResearchSource[] }>(
      "GET",
      `/api/workspaces/${encodeURIComponent(id)}/sources`,
    ),
  uploadToWorkspace: (id: string, name: string, contentBase64: string) =>
    request<Artifact>("POST", `/api/workspaces/${encodeURIComponent(id)}/uploads`, {
      name,
      content_base64: contentBase64,
    }),

  // Terminals
  createTerminal: () => request<TerminalCreated>("POST", "/api/terminals", {}),
  listTerminals: () => request<TerminalList>("GET", "/api/terminals"),
  deleteTerminal: (id: string) =>
    request<{ id: string; closed: boolean }>(
      "DELETE",
      `/api/terminals/${encodeURIComponent(id)}`,
    ),
};

export type Api = typeof api;
export type { PreviewCommand };
