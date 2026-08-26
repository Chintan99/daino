// Small typed fetch wrapper for the Daino REST API.
// All URLs are relative ("/api/...") so the app works same-origin in production
// and through the Vite dev proxy in development.

import type {
  Design,
  DesignList,
  FileRead,
  FileTree,
  FileWriteResult,
  GitDiff,
  GitStatus,
  Health,
  PreviewCommand,
  PreviewDetect,
  PreviewStatus,
  SearchResult,
  SessionList,
  SessionMessages,
  TerminalCreated,
  TerminalList,
  Workspace,
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
  workspace: () => request<Workspace>("GET", "/api/workspace"),

  // Sessions
  listSessions: () => request<SessionList>("GET", "/api/sessions"),
  createSession: (title: string) =>
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
