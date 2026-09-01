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
  RunInspectionRequest,
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

  // Preview
  previewDetect: () => request<PreviewDetect>("GET", "/api/preview/detect"),
  previewStatus: () => request<PreviewStatus>("GET", "/api/preview/status"),
  previewStart: (command: string, url: string) =>
    request<{ running: boolean; command: string; url: string }>(
      "POST",
      "/api/preview/start",
      { command, url },
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
  writeArtifact: (id: string, path: string, content: string) =>
    request<Artifact>("PUT", `/api/workspaces/${encodeURIComponent(id)}/artifact`, {
      path,
      content,
      author: "user",
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
