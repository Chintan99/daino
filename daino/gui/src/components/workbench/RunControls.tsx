import { useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { api } from "../../api/client";
import { qk, useWorkspaceSkills } from "../../api/hooks";
import type { Workspace, WorkspaceRun } from "../../api/types";

const LABEL: Record<WorkspaceRun["status"], string> = {
  pending: "Starting",
  running: "Running",
  paused: "Paused",
  waiting_for_user: "Needs you",
  waiting_for_approval: "Needs approval",
  completed: "Completed",
  failed: "Failed",
  cancelled: "Stopped",
};

/** Whether the executor is still attached — mirrors ``WorkspaceRun.active``. */
export function isActive(run: WorkspaceRun | null | undefined): boolean {
  return (
    !!run &&
    ["pending", "running", "paused", "waiting_for_user", "waiting_for_approval"].includes(
      run.status,
    )
  );
}

/**
 * Run the plan, and steer the run while it works.
 *
 * Sits above the plan rather than in a tab of its own: the thing being executed
 * and the controls for executing it are one object, and splitting them would
 * mean watching progress somewhere other than where the steps are.
 */
export function RunControls({
  workspace,
  run,
}: {
  workspace: Workspace;
  run: WorkspaceRun | null;
}) {
  const qc = useQueryClient();
  const [busy, setBusy] = useState(false);
  const [skill, setSkill] = useState("");
  const { data: skills } = useWorkspaceSkills();
  const active = isActive(run);
  const pending = workspace.tasks.filter((task) => task.status === "pending").length;

  const act = async (call: () => Promise<unknown>) => {
    setBusy(true);
    try {
      await call();
      await qc.invalidateQueries({ queryKey: qk.workspaceRun(workspace.id) });
      await qc.invalidateQueries({ queryKey: qk.workspaceItem(workspace.id) });
    } catch (err) {
      window.alert(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  const chosen = skills?.skills.find((item) => item.name === (run?.skill || skill));

  return (
    <div className={`ws-run ${run ? run.status : "idle"}`}>
      <div className="ws-run-head">
        <span className={`ws-run-state ${run ? run.status : "idle"}`}>
          {run ? LABEL[run.status] : "Not running"}
        </span>
        {run && (
          <span className="ws-run-progress">
            {run.completed_tasks} / {run.total_tasks} steps
          </span>
        )}
        <span className="ws-run-actions">
          {!active && (
            <button
              className="btn primary"
              disabled={busy || pending === 0}
              title={
                pending === 0
                  ? "Every step is already done or failed"
                  : "Work through the plan, one step at a time"
              }
              onClick={() =>
                void act(() => api.startWorkspaceRun(workspace.id, { skill }))
              }
            >
              Run Plan
            </button>
          )}
          {run && active && run.status !== "paused" && (
            <button
              className="btn subtle"
              disabled={busy}
              onClick={() => void act(() => api.pauseWorkspaceRun(run.id))}
            >
              Pause
            </button>
          )}
          {run && active && run.status === "paused" && (
            <button
              className="btn primary"
              disabled={busy}
              onClick={() => void act(() => api.resumeWorkspaceRun(run.id))}
            >
              Resume
            </button>
          )}
          {run && active && (
            <button
              className="btn subtle"
              disabled={busy}
              onClick={() => void act(() => api.stopWorkspaceRun(run.id))}
            >
              Stop
            </button>
          )}
        </span>
      </div>

      {!run && (
        <div className="ws-run-skill">
          <label htmlFor="ws-skill">Approach</label>
          <select
            id="ws-skill"
            className="input"
            value={skill}
            onChange={(e) => setSkill(e.target.value)}
          >
            <option value="">Chosen from the goal</option>
            {(skills?.skills ?? []).map((item) => (
              <option key={item.name} value={item.name}>
                {item.title}
              </option>
            ))}
          </select>
        </div>
      )}

      {chosen && run && (
        <div className="ws-run-note" title={chosen.description}>
          Using skill: <strong>{chosen.title}</strong>
        </div>
      )}

      {run?.pending_approval && (
        <div className="ws-approval">
          <div className="ws-approval-title">Daino needs approval</div>
          <div className="ws-approval-action">{run.pending_approval.action}</div>
          <div className="ws-approval-reason">{run.pending_approval.reason}</div>
          <div className="ws-approval-buttons">
            <button
              className="btn primary"
              disabled={busy}
              onClick={() =>
                void act(() =>
                  api.resolveRunApproval(run.id, run.pending_approval!.id, true),
                )
              }
            >
              Allow
            </button>
            <button
              className="btn subtle"
              disabled={busy}
              onClick={() =>
                void act(() =>
                  api.resolveRunApproval(run.id, run.pending_approval!.id, false),
                )
              }
            >
              Deny
            </button>
          </div>
        </div>
      )}

      {run && run.error && !run.pending_approval && (
        <div className={`ws-run-error ${run.status}`}>
          <span>{run.error}</span>
          {run.status === "waiting_for_user" && <FailureActions run={run} act={act} />}
        </div>
      )}

      {run?.status === "completed" && <Completion run={run} />}
    </div>
  );
}

/** Retry, skip, or leave it — the three honest answers to a failed step. */
function FailureActions({
  run,
  act,
}: {
  run: WorkspaceRun;
  act: (call: () => Promise<unknown>) => Promise<void>;
}) {
  const failed = [...run.steps].reverse().find((step) => step.kind === "task_failed");
  if (!failed?.task_id) return null;
  return (
    <span className="ws-run-recover">
      <button className="btn subtle" onClick={() => void act(() => api.retryRunTask(run.id, failed.task_id))}>
        Retry
      </button>
      <button className="btn subtle" onClick={() => void act(() => api.skipRunTask(run.id, failed.task_id))}>
        Skip
      </button>
      <span className="hint">…or tell Daino what to do differently in the chat.</span>
    </span>
  );
}

/** What the run produced, so the result is not only in the transcript. */
function Completion({ run }: { run: WorkspaceRun }) {
  const artifacts = Array.isArray(run.metadata.artifacts)
    ? (run.metadata.artifacts as string[])
    : [];
  const sources = typeof run.metadata.sources === "number" ? run.metadata.sources : 0;
  return (
    <div className="ws-run-summary">
      <div className="ws-run-summary-title">
        Run completed — {run.completed_tasks} of {run.total_tasks} steps
      </div>
      {artifacts.length > 0 && (
        <ul className="ws-run-summary-list">
          {artifacts.map((path) => (
            <li key={path}>{path.split("/").pop()}</li>
          ))}
        </ul>
      )}
      {sources > 0 && <div className="hint">{sources} sources recorded</div>}
    </div>
  );
}
