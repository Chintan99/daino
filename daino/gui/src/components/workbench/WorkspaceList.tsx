import { useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { api } from "../../api/client";
import { qk, useWorkspaceTemplates, useWorkspaces } from "../../api/hooks";
import { useUIStore } from "../../store/uiStore";
import { fmtDateTime } from "../insights/format";

/**
 * The workspaces in this project, and the button that starts a new one.
 *
 * A workspace is chosen by its goal far more often than by its name, so the
 * goal is on the card rather than hidden behind it.
 */
export function WorkspaceList() {
  const qc = useQueryClient();
  const { data, isLoading } = useWorkspaces();
  const { data: templates } = useWorkspaceTemplates();
  const active = useUIStore((s) => s.activeWorkspaceId);
  const setActive = useUIStore((s) => s.setActiveWorkspaceId);
  const [creating, setCreating] = useState(false);
  const [name, setName] = useState("");
  const [goal, setGoal] = useState("");
  const [kind, setKind] = useState("general");
  const [busy, setBusy] = useState(false);

  const workspaces = data?.workspaces ?? [];

  const create = async () => {
    if (!name.trim() || busy) return;
    setBusy(true);
    try {
      const made = await api.createWorkspace({ name, goal, kind });
      await qc.invalidateQueries({ queryKey: qk.workspaces });
      setActive(made.id);
      setCreating(false);
      setName("");
      setGoal("");
      setKind("general");
    } catch (err) {
      window.alert(
        `Could not create the workspace: ${err instanceof Error ? err.message : String(err)}`,
      );
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <div className="panel-header">
        Workspaces
        <span className="spacer" />
        <button
          className="btn icon"
          title="New workspace"
          onClick={() => setCreating((open) => !open)}
        >
          {creating ? "×" : "+"}
        </button>
      </div>

      {creating && (
        <div className="ws-new">
          <input
            className="input"
            autoFocus
            value={name}
            placeholder="Name"
            onChange={(e) => setName(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && void create()}
          />
          <textarea
            className="input"
            rows={2}
            value={goal}
            placeholder="What is this for?"
            onChange={(e) => setGoal(e.target.value)}
          />
          <select
            className="model-picker"
            value={kind}
            onChange={(e) => setKind(e.target.value)}
            title={templates?.templates.find((t) => t.name === kind)?.purpose}
          >
            {(templates?.templates ?? []).map((template) => (
              <option key={template.name} value={template.name}>
                {template.title}
              </option>
            ))}
          </select>
          <button
            className="btn primary"
            disabled={!name.trim() || busy}
            onClick={() => void create()}
          >
            Create
          </button>
        </div>
      )}

      <div className="scroll-y" style={{ flex: 1 }}>
        {isLoading && <div className="empty">Loading…</div>}
        {!isLoading && workspaces.length === 0 && !creating && (
          <div className="empty">
            No workspaces yet. Start one for a document, a piece of research, or
            anything that is not code.
          </div>
        )}
        {workspaces.map((workspace) => (
          <button
            key={workspace.id}
            className={`ws-card ${active === workspace.id ? "active" : ""}`}
            onClick={() => setActive(workspace.id)}
          >
            <div className="ws-card-head">
              <span className="ws-name">{workspace.name}</span>
              <span className="badge">{workspace.kind}</span>
            </div>
            {workspace.goal && <div className="ws-goal">{workspace.goal}</div>}
            <div className="ws-card-meta">
              <span>
                {workspace.done_count}/{workspace.task_count} done
              </span>
              <span>
                {workspace.artifact_count} doc
                {workspace.artifact_count === 1 ? "" : "s"}
              </span>
              {workspace.upload_count > 0 && (
                <span>{workspace.upload_count} uploaded</span>
              )}
              <span className="spacer" />
              <span className="muted">{fmtDateTime(workspace.updated_at)}</span>
            </div>
          </button>
        ))}
      </div>
    </>
  );
}
