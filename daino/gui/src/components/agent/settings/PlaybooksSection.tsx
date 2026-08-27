import { useState } from "react";
import type { AgentConfig } from "../../../api/types";
import { openFileInEditor } from "../../../lib/openFile";

/**
 * Engineering playbooks — the browser's `/playbooks`.
 *
 * These are D[Ai]NO's reusable procedures: staged plans with their own allowed
 * tools, approval points, verification and rollback steps. Built-in ones ship
 * with the package; a project adds its own as YAML under `.daino/playbooks/`,
 * and a project file with the same name replaces the built-in.
 */
export function PlaybooksSection({ config }: { config: AgentConfig }) {
  const [expanded, setExpanded] = useState<string | null>(null);

  if (config.playbooks.length === 0) {
    return (
      <div className="cfg-section">
        <div className="empty">No playbooks found.</div>
      </div>
    );
  }

  return (
    <div className="cfg-section">
      <div className="section-title">Playbooks · {config.playbooks.length}</div>
      <div className="muted field-hint">
        Add your own as YAML under <span className="mono">.daino/playbooks/</span>;
        a file with a built-in's name replaces it.
      </div>
      {config.playbooks.map((playbook) => {
        const open = expanded === playbook.name;
        return (
          <div className="pb-row" key={playbook.name}>
            <button
              className="pb-head"
              onClick={() => setExpanded(open ? null : playbook.name)}
            >
              <span className="grow">
                <span className="name">
                  {playbook.name}
                  {playbook.builtin ? (
                    <span className="badge">built-in</span>
                  ) : (
                    <span className="badge ok">project</span>
                  )}
                </span>
                <span className="detail">{playbook.purpose}</span>
              </span>
              <span className="muted mono">v{playbook.version}</span>
            </button>
            {open && (
              <div className="pb-body">
                <div className="muted">Stages</div>
                <ol className="pb-stages">
                  {playbook.stages.map((stage, index) => (
                    // eslint-disable-next-line react/no-array-index-key
                    <li key={`${playbook.name}-${index}`}>{stage}</li>
                  ))}
                </ol>
                {playbook.approval_points.length > 0 && (
                  <div className="muted field-hint">
                    Approval at: {playbook.approval_points.join(", ")}
                  </div>
                )}
                <div className="muted field-hint mono">
                  tools: {playbook.allowed_tools.join(", ")}
                </div>
                {playbook.relative_path && (
                  <button
                    className="btn sm"
                    onClick={() => void openFileInEditor(playbook.relative_path)}
                  >
                    Open YAML
                  </button>
                )}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}
