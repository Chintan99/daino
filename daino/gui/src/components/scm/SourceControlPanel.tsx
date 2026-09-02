import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useGitStatus } from "../../api/hooks";
import { api } from "../../api/client";
import { useEditorStore, diffTabId } from "../../store/editorStore";
import {
  openConflictInEditor,
  openDiffInEditor,
  openFileInEditor,
  openHunksInEditor,
} from "../../lib/openFile";
import { conflictTabId } from "../../store/editorStore";
import { BranchBar } from "./BranchBar";
import { CommitBox } from "./CommitBox";
import type { GitEntry } from "../../api/types";

type Group = "staged" | "modified" | "untracked";

function Section({
  title,
  group,
  entries,
  activeTabId,
  onRefresh,
}: {
  title: string;
  group: Group;
  entries: GitEntry[];
  activeTabId: string | null;
  onRefresh: () => void;
}) {
  if (entries.length === 0) return null;
  const staged = group === "staged";

  const act = async (fn: () => Promise<unknown>) => {
    await fn();
    onRefresh();
  };

  return (
    <div className="scm-section">
      <div className="scm-title">
        {title}
        <span className="badge">{entries.length}</span>
        <span className="grow" />
        {group !== "staged" && (
          <button
            className="btn icon"
            title={`Stage all ${title.toLowerCase()}`}
            onClick={() =>
              void act(() => api.gitStage(entries.map((e) => e.path)))
            }
          >
            ＋
          </button>
        )}
        {group === "staged" && (
          <button
            className="btn icon"
            title="Unstage all"
            onClick={() =>
              void act(() => api.gitUnstage(entries.map((e) => e.path)))
            }
          >
            −
          </button>
        )}
      </div>
      {entries.map((e) => {
        const mark = (e.status || title[0])[0];
        return (
          <div
            key={`${title}:${e.path}`}
            className={`scm-row ${
              activeTabId === diffTabId(e.path, staged) ? "active" : ""
            }`}
            onClick={() => openDiffInEditor(e.path, staged)}
            onDoubleClick={() => void openFileInEditor(e.path)}
            title={`${e.path} — click to diff, double-click to open`}
          >
            <span className={`git-mark git-${mark}`} style={{ marginLeft: 0 }}>
              {mark}
            </span>
            <span className="tree-name">{e.path}</span>
            <span className="acts">
              {group !== "untracked" && (
                <button
                  className="btn icon"
                  title={
                    staged
                      ? "Unstage part of this file"
                      : "Stage part of this file"
                  }
                  onClick={(ev) => {
                    ev.stopPropagation();
                    openHunksInEditor(e.path, staged);
                  }}
                >
                  ⁝
                </button>
              )}
              {staged ? (
                <button
                  className="btn icon"
                  title="Unstage the whole file"
                  onClick={(ev) => {
                    ev.stopPropagation();
                    void act(() => api.gitUnstage([e.path]));
                  }}
                >
                  −
                </button>
              ) : (
                <>
                  {group === "modified" && (
                    <button
                      className="btn icon"
                      title="Discard changes"
                      onClick={(ev) => {
                        ev.stopPropagation();
                        // CONFIRM: discarding cannot be undone from here.
                        if (
                          window.confirm(
                            `Discard all working-tree changes to ${e.path}?`,
                          )
                        )
                          void act(() => api.gitDiscard([e.path]));
                      }}
                    >
                      ↶
                    </button>
                  )}
                  <button
                    className="btn icon"
                    title="Stage the whole file"
                    onClick={(ev) => {
                      ev.stopPropagation();
                      void act(() => api.gitStage([e.path]));
                    }}
                  >
                    ＋
                  </button>
                </>
              )}
            </span>
          </div>
        );
      })}
    </div>
  );
}

export function SourceControlPanel() {
  const qc = useQueryClient();
  const { data: git, isLoading } = useGitStatus();
  const activeTabId = useEditorStore((s) => s.activeTabId);
  // Polled with the rest of the git queries, so a merge started from the
  // terminal shows up here without anyone reloading.
  const { data: merge } = useQuery({
    queryKey: ["git", "conflicts"],
    queryFn: api.gitConflicts,
  });

  const refresh = () => {
    void qc.invalidateQueries({ queryKey: ["git"] });
  };

  const clean =
    git?.repository &&
    git.staged.length === 0 &&
    git.modified.length === 0 &&
    git.untracked.length === 0;

  return (
    <div className="panel">
      <div className="panel-header">
        Source Control
        <span className="spacer" />
        <button className="btn icon" title="Refresh" onClick={refresh}>
          ⟳
        </button>
      </div>
      <div className="panel-body">
        {isLoading && <div className="empty">Loading…</div>}
        {git && !git.repository && (
          <div className="empty">Not a git repository</div>
        )}
        {git?.repository && (
          <>
            <BranchBar />
            {(merge?.conflicts.length ?? 0) > 0 && (
              <div className="scm-section">
                <div className="scm-title">
                  Conflicts
                  <span className="badge warn">{merge?.conflicts.length}</span>
                  <span className="grow" />
                  <button
                    className="btn icon"
                    title="Abandon the merge and go back"
                    onClick={() =>
                      void api.gitAbortMerge().then(refresh)
                    }
                  >
                    ✕
                  </button>
                </div>
                {merge?.conflicts.map((path) => (
                  <div
                    key={path}
                    className={`scm-row ${
                      activeTabId === conflictTabId(path) ? "active" : ""
                    }`}
                    onClick={() => openConflictInEditor(path)}
                    title={`${path} — click to resolve`}
                  >
                    <span className="scm-mark conflict">!</span>
                    <span className="scm-path ellipsis">{path}</span>
                  </div>
                ))}
              </div>
            )}
            {clean && !merge?.merging && <div className="empty">No changes</div>}
            <Section
              title="Staged"
              group="staged"
              entries={git.staged}
              activeTabId={activeTabId}
              onRefresh={refresh}
            />
            <Section
              title="Modified"
              group="modified"
              entries={git.modified}
              activeTabId={activeTabId}
              onRefresh={refresh}
            />
            <Section
              title="Untracked"
              group="untracked"
              entries={git.untracked.map((u) => ({ ...u, status: "?" }))}
              activeTabId={activeTabId}
              onRefresh={refresh}
            />
            <CommitBox />
          </>
        )}
      </div>
    </div>
  );
}
