import { useAgentStore } from "../../store/agentStore";
import { openDiffInEditor, openFileInEditor } from "../../lib/openFile";

/**
 * What the turn is editing, while it is editing it.
 *
 * The same shape as the closing changeset, so the running view and the final one
 * are recognisably one thing rather than two designs — the file currently being
 * written is the last row and is marked. Without this, a long turn shows a
 * scrolling list of individual edit cards and no answer to "how much has it
 * touched so far?".
 */
export function LiveChangeset() {
  const changes = useAgentStore((s) => s.liveChanges);
  const turnRunning = useAgentStore((s) => s.turnRunning);
  if (!turnRunning || changes.length === 0) return null;

  const added = changes.reduce((total, change) => total + change.added, 0);
  const removed = changes.reduce((total, change) => total + change.removed, 0);
  const currentPath = changes[changes.length - 1]?.path;

  return (
    <div className="changeset live">
      <div className="changeset-head">
        <span className="mark pulse" aria-hidden="true">
          ±
        </span>
        <span className="grow">
          <span className="title">
            Editing {changes.length} file{changes.length === 1 ? "" : "s"}
          </span>
          <span className="counts">
            <span className="added">+{added}</span>{" "}
            <span className="removed">-{removed}</span>
          </span>
        </span>
      </div>
      {changes.map((change) => (
        <div
          className={`changeset-row ${change.path === currentPath ? "current" : ""}`}
          key={change.path}
        >
          <button
            className="path"
            title={`Open ${change.path}`}
            onClick={() => void openFileInEditor(change.path)}
          >
            {change.path.includes("/") && (
              <span className="dir">
                {change.path.slice(0, change.path.lastIndexOf("/") + 1)}
              </span>
            )}
            <span className="name">
              {change.path.slice(
                change.path.includes("/") ? change.path.lastIndexOf("/") + 1 : 0,
              )}
            </span>
          </button>
          <span className="counts">
            <span className="added">+{change.added}</span>{" "}
            <span className="removed">-{change.removed}</span>
          </span>
          <button
            className="btn subtle sm"
            title="Open this file's diff"
            onClick={() => openDiffInEditor(change.path, false)}
          >
            diff
          </button>
        </div>
      ))}
    </div>
  );
}
