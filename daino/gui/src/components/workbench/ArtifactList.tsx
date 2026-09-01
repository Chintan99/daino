import { useQueryClient } from "@tanstack/react-query";
import { api } from "../../api/client";
import { qk } from "../../api/hooks";
import { useUIStore } from "../../store/uiStore";
import { openFileInEditor } from "../../lib/openFile";
import { confirmFor } from "../../store/dialogStore";
import type { Artifact, Workspace } from "../../api/types";
import { fmtDateTime } from "../insights/format";

/**
 * The documents in a workspace.
 *
 * Every row is a real file in the project, so alongside opening it here there
 * is always the option of opening it in CODE — which is the whole argument for
 * putting workspace folders in the repository rather than in a private store.
 */
export function ArtifactList({ workspace }: { workspace: Workspace }) {
  const qc = useQueryClient();
  const activePath = useUIStore((s) => s.activeArtifactPath);
  const setActivePath = useUIStore((s) => s.setActiveArtifactPath);

  const create = async () => {
    const name = window.prompt("New document (filename)", "notes.md");
    if (!name?.trim()) return;
    const path = name.trim().endsWith(".md") ? name.trim() : `${name.trim()}.md`;
    try {
      await api.writeArtifact(workspace.id, path, `# ${path.replace(/\.md$/, "")}\n\n`);
      await qc.invalidateQueries({ queryKey: qk.workspaceItem(workspace.id) });
      setActivePath(path);
    } catch (err) {
      window.alert(err instanceof Error ? err.message : String(err));
    }
  };

  const remove = async (artifact: Artifact) => {
    const ok = await confirmFor({
      title: "Delete document",
      message: `Delete ${artifact.path}? Its history is kept, so this can be undone from the file's revisions.`,
      confirmLabel: "Delete",
      danger: true,
    });
    if (!ok) return;
    await api.deleteArtifact(workspace.id, artifact.path);
    if (activePath === artifact.path) setActivePath(null);
    await qc.invalidateQueries({ queryKey: qk.workspaceItem(workspace.id) });
  };

  return (
    <div className="ws-artifacts">
      <div className="section-title">
        Documents
        <span className="spacer" />
        <button className="btn icon" title="New document" onClick={() => void create()}>
          +
        </button>
      </div>

      {workspace.artifacts.length === 0 && (
        <div className="empty">
          No documents yet. Create one, or ask the agent to draft it.
        </div>
      )}

      {workspace.artifacts.map((artifact) => (
        <div
          key={artifact.path}
          className={`ws-doc ${activePath === artifact.path ? "active" : ""}`}
          onClick={() => setActivePath(artifact.path)}
        >
          <div className="ws-doc-head">
            <span className="ws-doc-title">{artifact.title}</span>
            <span className="ws-doc-actions">
              <button
                className="btn icon"
                title="Open in CODE"
                onClick={(e) => {
                  e.stopPropagation();
                  void openFileInEditor(artifact.repo_path);
                }}
              >
                ↗
              </button>
              <button
                className="btn icon"
                title="Delete"
                onClick={(e) => {
                  e.stopPropagation();
                  void remove(artifact);
                }}
              >
                ×
              </button>
            </span>
          </div>
          <div className="mono muted ws-doc-path">{artifact.path}</div>
          {artifact.preview && <div className="ws-doc-preview">{artifact.preview}</div>}
          <div className="ws-doc-meta">
            <span>{fmtDateTime(artifact.updated_at)}</span>
            {artifact.revisions > 1 && <span>{artifact.revisions} versions</span>}
          </div>
        </div>
      ))}
    </div>
  );
}
