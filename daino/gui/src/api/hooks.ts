// TanStack Query hooks for Daino server state.

import {
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import { api } from "./client";
import type { Design, ReviewScope } from "./types";

export const qk = {
  health: ["health"] as const,
  projectInfo: ["project"] as const,
  sessions: ["sessions"] as const,
  sessionMessages: (id: string) => ["session", id, "messages"] as const,
  fileTree: (path: string) => ["files", "tree", path] as const,
  fileRead: (path: string) => ["files", "read", path] as const,
  search: (q: string) => ["files", "search", q] as const,
  gitStatus: ["git", "status"] as const,
  gitDiff: (path: string, staged: boolean) =>
    ["git", "diff", path, staged] as const,
  gitFile: (path: string, staged: boolean) =>
    ["git", "file", path, staged] as const,
  logs: (q: string) => ["logs", q] as const,
  mapPrompts: ["map", "prompts"] as const,
  mapTrace: (id: string) => ["map", "trace", id] as const,
  qaLatest: ["qa", "latest"] as const,
  qaHistory: ["qa", "history"] as const,
  reviewLatest: ["review", "latest"] as const,
  reviewHistory: ["review", "history"] as const,
  reviewSubject: (scope: string, baseRef: string) =>
    ["review", "subject", scope, baseRef] as const,
  reviewDiff: (path: string, scope: string, baseRef: string, reviewId: string) =>
    ["review", "diff", path, scope, baseRef, reviewId] as const,
  missions: ["missions"] as const,
  missionDetails: (id: string) => ["missions", id] as const,
  checkpoints: ["checkpoints"] as const,
  approvals: ["approvals"] as const,
  repository: ["repository"] as const,
  designs: ["designs"] as const,
  design: (id: string) => ["design", id] as const,
  previewDetect: ["preview", "detect"] as const,
  previewStatus: ["preview", "status"] as const,
  terminals: ["terminals"] as const,
  settings: ["settings"] as const,
  agentConfig: (sessionId: string) => ["agent", "config", sessionId] as const,
  memory: (key: string) => ["agent", "memory", key] as const,
  providerHealth: ["settings", "providers", "health"] as const,
  workspaces: ["workspaces"] as const,
  workspaceItem: (id: string) => ["workspaces", id] as const,
  workspaceArtifact: (id: string, path: string) =>
    ["workspaces", id, "artifact", path] as const,
  workspaceRevisions: (id: string, path: string) =>
    ["workspaces", id, "revisions", path] as const,
  workspaceTemplates: ["workspaces", "templates"] as const,
  workspaceRun: (id: string) => ["workspaces", id, "run"] as const,
  workspaceSkills: ["workspaces", "skills"] as const,
  workspaceChanges: (id: string) => ["workspaces", id, "changes"] as const,
  workspaceLinks: (id: string) => ["workspaces", id, "links"] as const,
  workspaceChangeDiff: (id: string, changeSetId: string, path: string) =>
    ["workspaces", id, "changes", changeSetId, path] as const,
};

export function useProjectInfo() {
  return useQuery({ queryKey: qk.projectInfo, queryFn: api.projectInfo });
}

export function useSessions() {
  return useQuery({ queryKey: qk.sessions, queryFn: api.listSessions });
}

export function useSessionMessages(id: string | null) {
  return useQuery({
    queryKey: qk.sessionMessages(id ?? ""),
    queryFn: () => api.sessionMessages(id as string),
    enabled: !!id,
  });
}

export function useFileTree(path: string, enabled = true) {
  return useQuery({
    queryKey: qk.fileTree(path),
    queryFn: () => api.fileTree(path),
    enabled,
  });
}

export function useSearch(q: string) {
  return useQuery({
    queryKey: qk.search(q),
    queryFn: () => api.search(q),
    enabled: q.trim().length > 0,
  });
}

export function useGitStatus() {
  return useQuery({ queryKey: qk.gitStatus, queryFn: api.gitStatus });
}

export function useGitDiff(path: string | null, staged = false) {
  return useQuery({
    queryKey: qk.gitDiff(path ?? "", staged),
    queryFn: () => api.gitDiff(path as string, staged),
    enabled: !!path,
  });
}

export function useGitFile(path: string | null, staged = false) {
  return useQuery({
    queryKey: qk.gitFile(path ?? "", staged),
    queryFn: () => api.gitFile(path as string, staged),
    enabled: !!path,
  });
}

// ---- Engineering evidence ----

export function useLogs(q: string, refetchMs = 4000) {
  return useQuery({
    queryKey: qk.logs(q),
    queryFn: () => api.logs(q),
    refetchInterval: refetchMs,
  });
}

export function useMapPrompts() {
  return useQuery({ queryKey: qk.mapPrompts, queryFn: () => api.mapPrompts() });
}

export function useMapTrace(missionId: string | null) {
  return useQuery({
    queryKey: qk.mapTrace(missionId ?? ""),
    queryFn: () => api.mapTrace(missionId as string),
    enabled: !!missionId,
  });
}

/** Poll only while a scan is in flight; a finished report never changes. */
export function useQALatest() {
  return useQuery({
    queryKey: qk.qaLatest,
    queryFn: api.qaLatest,
    refetchInterval: (query) => (query.state.data?.running ? 1500 : false),
  });
}

export function useQAHistory() {
  return useQuery({ queryKey: qk.qaHistory, queryFn: () => api.qaHistory() });
}

export function useMissions() {
  return useQuery({ queryKey: qk.missions, queryFn: () => api.missions() });
}

export function useMissionDetails(id: string | null) {
  return useQuery({
    queryKey: qk.missionDetails(id ?? ""),
    queryFn: () => api.missionDetails(id as string),
    enabled: !!id,
  });
}

export function useCheckpoints() {
  return useQuery({ queryKey: qk.checkpoints, queryFn: () => api.checkpoints() });
}

export function useApprovals() {
  return useQuery({ queryKey: qk.approvals, queryFn: () => api.approvals() });
}

export function useRepository() {
  return useQuery({ queryKey: qk.repository, queryFn: api.repository });
}

export function useDesigns() {
  return useQuery({ queryKey: qk.designs, queryFn: api.listDesigns });
}

export function useDesign(id: string | null) {
  return useQuery({
    queryKey: qk.design(id ?? ""),
    queryFn: () => api.getDesign(id as string),
    enabled: !!id,
  });
}

export function usePreviewDetect() {
  return useQuery({ queryKey: qk.previewDetect, queryFn: api.previewDetect });
}

export function usePreviewStatus(pollMs = 2000) {
  return useQuery({
    queryKey: qk.previewStatus,
    queryFn: api.previewStatus,
    refetchInterval: pollMs,
  });
}

export function useTerminals() {
  return useQuery({ queryKey: qk.terminals, queryFn: api.listTerminals });
}

/** Session autonomy, instructions, memory counts, playbooks — one request. */
export function useAgentConfig(sessionId: string | null) {
  return useQuery({
    queryKey: qk.agentConfig(sessionId ?? ""),
    queryFn: () => api.agentConfig(sessionId ?? ""),
    enabled: !!sessionId,
  });
}

export function useMemory(params: { q?: string; type?: string; scope?: string }) {
  const key = JSON.stringify(params);
  return useQuery({
    queryKey: qk.memory(key),
    queryFn: () => api.listMemory(params),
  });
}

export function useSettings() {
  return useQuery({ queryKey: qk.settings, queryFn: api.settings });
}

// ---- Mutations ----

/**
 * Change project/agent configuration.
 *
 * The server answers with the whole settings payload, so the cache is seeded
 * from the response instead of re-fetching what was just written.
 */
export function useSettingsMutation() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: api.patchSettings,
    onSuccess: (data) => {
      qc.setQueryData(qk.settings, data);
      // Routing changes which model answers the next turn.
      qc.invalidateQueries({ queryKey: qk.projectInfo });
    },
  });
}

export function useFileMutations() {
  const qc = useQueryClient();
  const invalidateTrees = () =>
    qc.invalidateQueries({ queryKey: ["files", "tree"] });

  const create = useMutation({
    mutationFn: (v: { path: string; is_dir: boolean }) =>
      api.createFile(v.path, v.is_dir),
    onSuccess: invalidateTrees,
  });
  const rename = useMutation({
    mutationFn: (v: { source: string; dest: string }) =>
      api.renameFile(v.source, v.dest),
    onSuccess: invalidateTrees,
  });
  const remove = useMutation({
    mutationFn: (path: string) => api.deleteFile(path),
    onSuccess: invalidateTrees,
  });
  return { create, rename, remove };
}

export function useDesignMutations(designId: string | null) {
  const qc = useQueryClient();
  const setDesign = (d: Design) => {
    if (designId) qc.setQueryData(qk.design(designId), d);
    qc.invalidateQueries({ queryKey: qk.designs });
  };

  const addNode = useMutation({
    mutationFn: (v: {
      label: string;
      node_type: string;
      x: number;
      y: number;
    }) => api.addNode(designId as string, v),
    onSuccess: setDesign,
  });
  const patchNode = useMutation({
    mutationFn: (v: {
      nodeId: string;
      body: {
        label?: string;
        node_type?: string;
        x?: number;
        y?: number;
        data?: Record<string, unknown>;
      };
    }) => api.patchNode(designId as string, v.nodeId, v.body),
    onSuccess: setDesign,
  });
  const deleteNode = useMutation({
    mutationFn: (nodeId: string) => api.deleteNode(designId as string, nodeId),
    onSuccess: setDesign,
  });
  const addEdge = useMutation({
    mutationFn: (v: { source: string; target: string; label?: string }) =>
      api.addEdge(designId as string, v),
    onSuccess: setDesign,
  });
  const deleteEdge = useMutation({
    mutationFn: (edgeId: string) => api.deleteEdge(designId as string, edgeId),
    onSuccess: setDesign,
  });

  return { addNode, patchNode, deleteNode, addEdge, deleteEdge };
}

export { useQueryClient };

// ---- Workspaces ----

export function useWorkspaces(includeArchived = false) {
  return useQuery({
    queryKey: qk.workspaces,
    queryFn: () => api.workspaces(includeArchived),
  });
}

export function useWorkspaceItem(id: string | null) {
  return useQuery({
    queryKey: qk.workspaceItem(id ?? ""),
    queryFn: () => api.workspace(id as string),
    enabled: !!id,
  });
}

export function useArtifact(workspaceId: string | null, path: string | null) {
  return useQuery({
    queryKey: qk.workspaceArtifact(workspaceId ?? "", path ?? ""),
    queryFn: () => api.readArtifact(workspaceId as string, path as string),
    enabled: !!workspaceId && !!path,
  });
}

export function useArtifactRevisions(workspaceId: string | null, path: string | null) {
  return useQuery({
    queryKey: qk.workspaceRevisions(workspaceId ?? "", path ?? ""),
    queryFn: () => api.artifactRevisions(workspaceId as string, path as string),
    enabled: !!workspaceId && !!path,
  });
}

/**
 * The run executing this workspace's plan, or the last one to have run.
 *
 * Polled slowly as a backstop only: progress arrives as ``WorkspaceRunUpdated``
 * events on the session socket, which invalidate this key the moment anything
 * moves.
 */
export function useWorkspaceRun(id: string | null) {
  return useQuery({
    queryKey: qk.workspaceRun(id ?? ""),
    queryFn: () => api.workspaceRun(id as string),
    enabled: !!id,
    refetchInterval: 15_000,
  });
}

/** How this workspace's outputs relate, and which have fallen behind. */
export function useWorkspaceLinks(id: string | null) {
  return useQuery({
    queryKey: qk.workspaceLinks(id ?? ""),
    queryFn: () => api.workspaceLinks(id as string),
    enabled: !!id,
  });
}

/** What the agent changed, grouped by the step that changed it. */
export function useWorkspaceChanges(id: string | null) {
  return useQuery({
    queryKey: qk.workspaceChanges(id ?? ""),
    queryFn: () => api.workspaceChanges(id as string),
    enabled: !!id,
  });
}

/** One artifact's before/after, both sides read from the workspace history. */
export function useChangeDiff(
  workspaceId: string | null,
  changeSetId: string | null,
  path: string | null,
) {
  return useQuery({
    queryKey: qk.workspaceChangeDiff(workspaceId ?? "", changeSetId ?? "", path ?? ""),
    queryFn: () =>
      api.workspaceChangeDiff(workspaceId as string, changeSetId as string, path as string),
    enabled: !!workspaceId && !!changeSetId && !!path,
  });
}

/** Every way of working available here; they change only on upgrade. */
export function useWorkspaceSkills() {
  return useQuery({
    queryKey: qk.workspaceSkills,
    queryFn: api.workspaceSkills,
    staleTime: Infinity,
  });
}

/** The work types a new workspace can start from; they change only on upgrade. */
export function useWorkspaceTemplates() {
  return useQuery({
    queryKey: qk.workspaceTemplates,
    queryFn: api.workspaceTemplates,
    staleTime: Infinity,
  });
}

// ---- Change review ----

/** Poll only while a review is in flight; a finished one never changes. */
export function useReviewLatest() {
  return useQuery({
    queryKey: qk.reviewLatest,
    queryFn: api.reviewLatest,
    refetchInterval: (query) => (query.state.data?.running ? 1500 : false),
  });
}

export function useReviewHistory() {
  return useQuery({ queryKey: qk.reviewHistory, queryFn: () => api.reviewHistory() });
}

/**
 * What a review of this scope would cover.
 *
 * Resolved before anything runs, so the view can show the size of the change
 * and surface an unresolvable base immediately rather than after a failed run.
 */
export function useReviewSubject(scope: ReviewScope, baseRef: string) {
  return useQuery({
    queryKey: qk.reviewSubject(scope, baseRef),
    queryFn: () => api.reviewSubject(scope, baseRef),
    retry: false,
  });
}

export function useReviewFileDiff(
  path: string | null,
  scope: ReviewScope,
  baseRef: string,
  reviewId = "",
) {
  return useQuery({
    queryKey: qk.reviewDiff(path ?? "", scope, baseRef, reviewId),
    queryFn: () => api.reviewFileDiff(path as string, scope, baseRef, reviewId),
    enabled: !!path,
  });
}

