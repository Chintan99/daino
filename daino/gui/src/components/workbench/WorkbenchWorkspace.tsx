import { useEffect, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { api } from "../../api/client";
import { qk, useWorkspaceItem } from "../../api/hooks";
import { useUIStore, type WorkbenchView } from "../../store/uiStore";
import { confirmFor } from "../../store/dialogStore";
import { WorkspaceList } from "./WorkspaceList";
import { ArtifactList } from "./ArtifactList";
import { ArtifactView } from "./ArtifactView";
import { TaskList } from "./TaskList";
import { UploadPanel } from "./UploadPanel";
import { SourcesPanel } from "./SourcesPanel";
import { ChangesPanel } from "./ChangesPanel";
import { LinksPanel } from "./LinksPanel";

const VIEWS: { id: WorkbenchView; label: string; hint: string }[] = [
  { id: "documents", label: "DOCUMENTS", hint: "What this workspace is producing" },
  { id: "tasks", label: "PLAN", hint: "The steps, editable by you and the agent" },
  { id: "uploads", label: "UPLOADS", hint: "Files brought in, and the text read from them" },
  { id: "sources", label: "SOURCES", hint: "Every page the agent read while researching" },
  { id: "changes", label: "CHANGES", hint: "What Daino changed, grouped by the step that changed it" },
];

/**
 * Knowledge work: documents, research, planning, and analysis.
 *
 * The sibling of CODE (software) and DESIGN (visual). What makes it Daino's
 * rather than a generic notebook is that a workspace is a real folder in the
 * project — its documents are greppable, indexable, diffable, and openable in
 * CODE, and the agent needs no special file tools to write them.
 *
 * The conversation is the shared agent panel, re-pointed at the selected
 * workspace's session. One composer, one approval flow, one set of slash
 * commands; the workspace just decides which conversation it is talking to.
 */
export function WorkbenchWorkspace() {
  const qc = useQueryClient();
  const view = useUIStore((s) => s.workbenchView);
  const setView = useUIStore((s) => s.setWorkbenchView);
  const activeId = useUIStore((s) => s.activeWorkspaceId);
  const setActiveId = useUIStore((s) => s.setActiveWorkspaceId);
  const setSessionTarget = useUIStore((s) => s.setSessionTarget);
  const { data: workspace } = useWorkspaceItem(activeId);
  const [editingGoal, setEditingGoal] = useState(false);
  const [goal, setGoal] = useState("");

  // The session this tab pointed the agent at, so leaving restores what CODE
  // was talking to rather than stranding the panel on a workspace thread.
  const borrowed = useRef<string | null>(null);

  useEffect(() => {
    if (!workspace) return;
    if (borrowed.current === null) {
      borrowed.current = useUIStore.getState().sessionTarget;
    }
    if (workspace.session_id) {
      setSessionTarget(workspace.session_id);
      return;
    }
    // A workspace with no conversation yet gets one, attached so its history
    // accumulates across every future session.
    let live = true;
    void (async () => {
      const created = await api.createSession(workspace.name);
      if (!live) return;
      await api.attachWorkspaceSession(workspace.id, created.id);
      setSessionTarget(created.id);
      await qc.invalidateQueries({ queryKey: qk.workspaceItem(workspace.id) });
    })();
    return () => {
      live = false;
    };
  }, [workspace?.id, workspace?.session_id, workspace?.name, setSessionTarget, qc, workspace]);

  useEffect(
    () => () => {
      if (borrowed.current !== null) {
        useUIStore.getState().setSessionTarget(borrowed.current);
        borrowed.current = null;
      }
    },
    [],
  );

  const saveGoal = async () => {
    setEditingGoal(false);
    if (!workspace || goal === workspace.goal) return;
    await api.updateWorkspace(workspace.id, { goal });
    await qc.invalidateQueries({ queryKey: qk.workspaceItem(workspace.id) });
    await qc.invalidateQueries({ queryKey: qk.workspaces });
  };

  const remove = async () => {
    if (!workspace) return;
    const ok = await confirmFor({
      title: `Delete "${workspace.name}"`,
      message:
        `The folder ${workspace.folder} and everything in it is kept — only the ` +
        "workspace entry is removed. Delete the files yourself if you want them gone.",
      confirmLabel: "Remove workspace",
      danger: true,
    });
    if (!ok) return;
    await api.deleteWorkspace(workspace.id);
    setActiveId(null);
    await qc.invalidateQueries({ queryKey: qk.workspaces });
  };

  return (
    <div className="insights">
      <div className="toolbar">
        {workspace ? (
          <>
            <button
              className="btn subtle sm"
              onClick={() => setActiveId(null)}
              title="Back to all workspaces"
            >
              ‹ All
            </button>
            <span className="ws-heading">{workspace.name}</span>
            <span className="badge">{workspace.kind}</span>
            <div className="segmented">
              {VIEWS.map((item) => (
                <button
                  key={item.id}
                  className={view === item.id ? "active" : ""}
                  onClick={() => setView(item.id)}
                  title={item.hint}
                >
                  {item.label}
                </button>
              ))}
            </div>
            <span className="grow" />
            <span className="mono muted" title="Everything here is a real file in the project">
              {workspace.folder}/
            </span>
            <button className="btn subtle sm" onClick={() => void remove()}>
              Remove
            </button>
          </>
        ) : (
          <>
            <span className="ws-heading">Workspaces</span>
            <span className="grow" />
            <span className="muted" style={{ fontSize: "var(--fs-11)" }}>
              Documents, research, planning, and analysis — the work that is not code
            </span>
          </>
        )}
      </div>

      <div className="insights-body">
        <div className="split">
          <div className="split-left" style={{ width: 300 }}>
            {workspace ? (
              <>
                <div className="ws-goal-box">
                  <div className="section-title">Goal</div>
                  {editingGoal ? (
                    <textarea
                      className="input"
                      autoFocus
                      rows={3}
                      value={goal}
                      onChange={(e) => setGoal(e.target.value)}
                      onBlur={() => void saveGoal()}
                    />
                  ) : (
                    <div
                      className={`ws-goal-text ${workspace.goal ? "" : "muted"}`}
                      onClick={() => {
                        setGoal(workspace.goal);
                        setEditingGoal(true);
                      }}
                      title="Click to edit"
                    >
                      {workspace.goal || "What is this workspace for?"}
                    </div>
                  )}
                </div>
                <div className="scroll-y" style={{ flex: 1 }}>
                  {(view === "documents" || view === "changes") && (
                    <>
                      <ArtifactList workspace={workspace} />
                      <LinksPanel workspace={workspace} />
                    </>
                  )}
                  {view === "tasks" && <TaskList workspace={workspace} />}
                  {view === "uploads" && <UploadPanel workspace={workspace} />}
                  {view === "sources" && <SourcesPanel workspace={workspace} />}
                </div>
              </>
            ) : (
              <WorkspaceList />
            )}
          </div>

          <div className="split-right">
            {workspace && view === "changes" ? (
              // Reviewing wants the width: a diff in a 300px column is a
              // column of fragments, not a change anyone can judge.
              <div className="scroll-y" style={{ flex: 1 }}>
                <ChangesPanel workspace={workspace} />
              </div>
            ) : workspace ? (
              <ArtifactView workspace={workspace} />
            ) : (
              <div className="empty" style={{ margin: "auto", maxWidth: 460 }}>
                Pick a workspace, or start one. Give Daino a goal, bring in the
                files it needs, and the documents it writes land in your project
                as ordinary files.
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
