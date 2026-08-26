import { useQueryClient } from "@tanstack/react-query";
import { useGitStatus } from "../../api/hooks";
import { api } from "../../api/client";
import { useEditorStore, diffTabId } from "../../store/editorStore";
import { openDiffInEditor, openFileInEditor } from "../../lib/openFile";
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
              {staged ? (
                <button
                  className="btn icon"
                  title="Unstage"
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
                    title="Stage"
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
            <div
              style={{
                padding: "7px 10px",
                borderBottom: "1px solid var(--border)",
              }}
            >
              <span className="badge info">⑃ {git.branch || "detached"}</span>
            </div>
            {clean && <div className="empty">No changes</div>}
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
          </>
        )}
      </div>
    </div>
  );
}
