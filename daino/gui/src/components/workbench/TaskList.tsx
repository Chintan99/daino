import { useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { api } from "../../api/client";
import { qk, useWorkspaceRun } from "../../api/hooks";
import type { Workspace, WorkspaceTask, WorkspaceTaskStatus } from "../../api/types";
import { RunControls, isActive } from "./RunControls";
import { RunTimeline } from "./RunTimeline";

/** Clicking a task walks it round the cycle rather than opening a menu. */
const NEXT: Record<WorkspaceTaskStatus, WorkspaceTaskStatus> = {
  pending: "in_progress",
  in_progress: "completed",
  completed: "pending",
  failed: "pending",
};

const MARK: Record<WorkspaceTaskStatus, string> = {
  pending: "○",
  in_progress: "◐",
  completed: "●",
  failed: "✗",
};

/**
 * The plan, editable by you and by the agent.
 *
 * This is the piece the existing checklist never was: session todos have no id,
 * no order, and are wiped between turns, so they could show progress but never
 * hold a plan. These persist, reorder, and can be reopened after completion.
 */
export function TaskList({ workspace }: { workspace: Workspace }) {
  const qc = useQueryClient();
  const { data: runData } = useWorkspaceRun(workspace.id);
  const run = runData?.run ?? null;
  const running = isActive(run);
  const [adding, setAdding] = useState("");
  const [editing, setEditing] = useState<string | null>(null);
  const [draft, setDraft] = useState("");

  const refresh = () =>
    qc.invalidateQueries({ queryKey: qk.workspaceItem(workspace.id) });

  const act = async (run: () => Promise<unknown>) => {
    try {
      await run();
      await refresh();
      await qc.invalidateQueries({ queryKey: qk.workspaces });
    } catch (err) {
      window.alert(err instanceof Error ? err.message : String(err));
    }
  };

  const add = () => {
    const content = adding.trim();
    if (!content) return;
    setAdding("");
    void act(() => api.addWorkspaceTask(workspace.id, content));
  };

  const commitEdit = (task: WorkspaceTask) => {
    const content = draft.trim();
    setEditing(null);
    if (!content || content === task.content) return;
    void act(() => api.updateWorkspaceTask(workspace.id, task.id, { content }));
  };

  const move = (index: number, delta: number) => {
    const order = workspace.tasks.map((item) => item.id);
    const target = index + delta;
    if (target < 0 || target >= order.length) return;
    [order[index], order[target]] = [order[target], order[index]];
    void act(() => api.reorderWorkspaceTasks(workspace.id, order));
  };

  const done = workspace.tasks.filter((item) => item.status === "completed").length;

  return (
    <div className="ws-tasks">
      <div className="section-title">
        Plan — {done}/{workspace.tasks.length} done
      </div>

      <RunControls workspace={workspace} run={run} />

      {workspace.tasks.length === 0 && (
        <div className="empty">
          No steps yet. Add them here, or ask the agent to plan the work.
        </div>
      )}

      <ul className="ws-task-list">
        {workspace.tasks.map((task, index) => (
          <li
            key={task.id}
            className={`ws-task ${task.status}${
              run?.current_task_id === task.id ? " current" : ""
            }`}
          >
            <button
              className="ws-task-mark"
              disabled={running}
              title={
                running
                  ? "The run owns the plan while it is working"
                  : `Mark ${NEXT[task.status].replace("_", " ")}`
              }
              onClick={() =>
                void act(() =>
                  api.updateWorkspaceTask(workspace.id, task.id, {
                    status: NEXT[task.status],
                  }),
                )
              }
            >
              {MARK[task.status]}
            </button>

            {editing === task.id ? (
              <input
                className="input"
                autoFocus
                value={draft}
                onChange={(e) => setDraft(e.target.value)}
                onBlur={() => commitEdit(task)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") commitEdit(task);
                  if (e.key === "Escape") setEditing(null);
                }}
              />
            ) : (
              <span
                className="ws-task-text"
                onDoubleClick={() => {
                  setEditing(task.id);
                  setDraft(task.content);
                }}
                title="Double-click to edit"
              >
                {task.content}
              </span>
            )}

            {task.error && <span className="ws-task-error">{task.error}</span>}

            <span className="ws-task-actions">
              <button className="btn icon" title="Move up" onClick={() => move(index, -1)}>
                ↑
              </button>
              <button
                className="btn icon"
                title="Move down"
                onClick={() => move(index, 1)}
              >
                ↓
              </button>
              <button
                className="btn icon"
                title="Delete"
                onClick={() =>
                  void act(() => api.deleteWorkspaceTask(workspace.id, task.id))
                }
              >
                ×
              </button>
            </span>
          </li>
        ))}
      </ul>

      <div className="ws-task-add">
        <input
          className="input"
          value={adding}
          placeholder="Add a step"
          onChange={(e) => setAdding(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && add()}
        />
        <button className="btn subtle" disabled={!adding.trim()} onClick={add}>
          Add
        </button>
      </div>

      {run && <RunTimeline run={run} />}
    </div>
  );
}
