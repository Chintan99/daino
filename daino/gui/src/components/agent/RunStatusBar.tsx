import { useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { api } from "../../api/client";
import {
  qk,
  useWorkspaceItem,
  useWorkspaceRun,
  useWorkspaceSkills,
} from "../../api/hooks";
import { useUIStore } from "../../store/uiStore";

/**
 * What the run is doing, and what Daino is currently working from.
 *
 * The panel itself is unchanged: this is a strip, not an orchestration
 * dashboard. It exists for two reasons. A user who types into the chat while a
 * plan is running has to be able to see that a plan *is* running — otherwise
 * the reply "plan updated from your instruction" arrives from nowhere. And a
 * user who cannot see what the agent considers relevant has no way to correct
 * it except by guessing.
 */
export function RunStatusBar() {
  const qc = useQueryClient();
  const workspaceId = useUIStore((s) => s.activeWorkspaceId);
  const tab = useUIStore((s) => s.activeWorkspaceTab);
  const inWorkspace = tab === "workspace" ? workspaceId : null;
  const { data: runData } = useWorkspaceRun(inWorkspace);
  const { data: workspace } = useWorkspaceItem(inWorkspace);
  const run = runData?.run ?? null;
  if (!workspace) return null;

  const live =
    run && ["running", "pending", "waiting_for_approval", "paused"].includes(run.status)
      ? run
      : null;

  return (
    <>
      {live && <RunStrip run={live} onPaused={() => qc.invalidateQueries({
        queryKey: qk.workspaceRun(live.workspace_id),
      })} />}
      <ContextStrip workspaceId={workspace.id} />
    </>
  );
}

function RunStrip({
  run,
  onPaused,
}: {
  run: NonNullable<ReturnType<typeof useWorkspaceRun>["data"]>["run"];
  onPaused: () => void;
}) {
  if (!run) return null;
  const current = [...run.steps].reverse().find((step) => step.kind === "task_started");
  const label =
    run.status === "waiting_for_approval"
      ? "NEEDS APPROVAL"
      : run.status === "paused"
        ? "PAUSED"
        : "RUNNING";
  const pause = async () => {
    try {
      await api.pauseWorkspaceRun(run.id);
    } finally {
      onPaused();
    }
  };
  return (
    <div className={`agent-run-bar ${run.status}`}>
      <span className="agent-run-state">{label}</span>
      <span className="agent-run-task">
        Step {Math.min(run.completed_tasks + 1, run.total_tasks)} of {run.total_tasks}
        {current ? ` — ${current.message}` : ""}
      </span>
      {run.status !== "paused" && (
        <button className="btn icon" title="Pause after this step" onClick={() => void pause()}>
          ⏸
        </button>
      )}
    </div>
  );
}

/**
 * What Daino is working from right now.
 *
 * Built entirely from state the panel already has — the workspace, the run, and
 * the open document — so showing it costs nothing and cannot drift from what
 * the agent is actually handed.
 */
function ContextStrip({ workspaceId }: { workspaceId: string }) {
  const [open, setOpen] = useState(false);
  const { data: workspace } = useWorkspaceItem(workspaceId);
  const { data: runData } = useWorkspaceRun(workspaceId);
  const { data: skills } = useWorkspaceSkills();
  const artifactPath = useUIStore((s) => s.activeArtifactPath);
  if (!workspace) return null;

  const run = runData?.run ?? null;
  const current = workspace.tasks.find((task) => task.id === run?.current_task_id);
  const skill = skills?.skills.find((item) => item.name === run?.skill);
  const working = Array.from(
    new Set(
      [
        artifactPath ?? "",
        ...(run?.steps ?? [])
          .filter((step) => step.kind === "artifact")
          .slice(-4)
          .map((step) => String(step.detail?.path ?? "").split("/").pop() ?? ""),
        ...workspace.uploads.filter((item) => item.extracted_path).map((item) => item.path),
      ].filter(Boolean),
    ),
  ).slice(0, 5);

  return (
    <div className="agent-context">
      <button className="agent-context-head" onClick={() => setOpen(!open)}>
        <span className="agent-context-label">CONTEXT</span>
        <span className="agent-context-goal">{workspace.goal || workspace.name}</span>
        <span className="agent-context-chevron">{open ? "▾" : "▸"}</span>
      </button>
      {open && (
        <dl className="agent-context-body">
          {current && (
            <>
              <dt>Current step</dt>
              <dd>{current.content}</dd>
            </>
          )}
          {working.length > 0 && (
            <>
              <dt>Working with</dt>
              <dd>{working.join(", ")}</dd>
            </>
          )}
          {skill && (
            <>
              <dt>Skill</dt>
              <dd>{skill.title}</dd>
            </>
          )}
          <dt>Folder</dt>
          <dd className="mono">{workspace.folder}/</dd>
        </dl>
      )}
    </div>
  );
}
